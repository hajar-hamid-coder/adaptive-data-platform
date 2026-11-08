"""SQL connector: incremental extraction driven by a persisted watermark.

Rows with ``cursor_column > watermark`` are read (strictly; first run = everything), ordered by
the cursor. The watermark advances to the max cursor read only after the whole read succeeded.
It is stored as text: integers as ``str(int)``, dates/timestamps as ISO 8601, others as plain
text; it is parsed back according to the reflected type of the cursor column.
Caveat: strict ``>`` means a row inserted later with a cursor value <= watermark is not seen.
Identifiers are validated and resolved through reflection; values are bound parameters.
"""

import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Engine,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    insert,
    select,
    update,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.types import TypeEngine

from adp.connectors.files import RawTable
from adp.errors import IngestionError
from adp.logging import get_logger

log = get_logger(__name__)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_PARSERS: list[tuple[type, Callable[[str], Any]]] = [
    (Integer, int),
    (DateTime, datetime.fromisoformat),
    (Date, date.fromisoformat),
    (Numeric, Decimal),
]


def _check_ident(name: str, what: str) -> str:
    if not isinstance(name, str) or not _IDENT.fullmatch(name):
        raise IngestionError(f"invalid {what} name: {name!r}")
    return name


def _to_str(v: Any) -> str | None:
    return None if v is None else str(v)


def _dump(v: Any) -> str:
    return v.isoformat() if isinstance(v, date | datetime) else str(v)


def _parser(col_type: TypeEngine[Any]) -> Callable[[str], Any]:
    for kind, fn in _PARSERS:
        if isinstance(col_type, kind):
            return fn
    return str


class WatermarkStore:
    """Persist ``adp_watermarks(source_name PK, cursor_column, last_value, updated_at)``."""

    def __init__(self, engine: Engine, table_name: str = "adp_watermarks") -> None:
        self._engine = engine
        md = MetaData()
        self._t = Table(
            _check_ident(table_name, "watermark table"),
            md,
            Column("source_name", String(255), primary_key=True),
            Column("cursor_column", String(64), nullable=False),
            Column("last_value", String, nullable=False),
            Column("updated_at", DateTime(timezone=True), nullable=False),
        )
        self._md = md

    def get(self, source_name: str) -> tuple[str, str] | None:
        """Return ``(cursor_column, last_value)`` or None if the source was never read."""
        try:
            self._md.create_all(self._engine)
            with self._engine.connect() as c:
                row = c.execute(
                    select(self._t.c.cursor_column, self._t.c.last_value).where(
                        self._t.c.source_name == source_name
                    )
                ).first()
        except SQLAlchemyError as e:
            raise IngestionError(f"watermark read failed ({type(e).__name__})") from e
        return None if row is None else (row[0], row[1])

    def set(self, source_name: str, cursor_column: str, value: str) -> None:
        """Upsert the watermark (update, then insert if absent) in one transaction."""
        vals = {
            "cursor_column": cursor_column,
            "last_value": value,
            "updated_at": datetime.now(UTC),
        }
        try:
            self._md.create_all(self._engine)
            with self._engine.begin() as c:
                res = c.execute(
                    update(self._t).where(self._t.c.source_name == source_name).values(**vals)
                )
                if res.rowcount == 0:
                    c.execute(insert(self._t).values(source_name=source_name, **vals))
        except SQLAlchemyError as e:
            raise IngestionError(f"watermark write failed ({type(e).__name__})") from e


def extract_incremental(
    engine: Engine,
    table: str,
    *,
    cursor_column: str,
    source_name: str,
    watermark_store: WatermarkStore,
    columns: list[str] | None = None,
    batch_size: int = 10_000,
) -> RawTable:
    """Read rows newer than the stored watermark, then advance it. Raises IngestionError."""
    _check_ident(table, "table")
    _check_ident(cursor_column, "cursor column")
    for c in columns or []:
        _check_ident(c, "column")
    if batch_size < 1:
        raise IngestionError("batch_size must be >= 1")

    try:
        tbl = Table(table, MetaData(), autoload_with=engine)
    except SQLAlchemyError as e:
        raise IngestionError(f"{table}: cannot reflect table ({type(e).__name__})") from e
    for name in [cursor_column, *(columns or [])]:
        if name not in tbl.c:
            raise IngestionError(f"{table}: unknown column {name!r}")

    names = columns or [c.name for c in tbl.c]
    selected = names if cursor_column in names else [*names, cursor_column]
    cur = tbl.c[cursor_column]
    parse = _parser(cur.type)

    stmt = select(*(tbl.c[n] for n in selected)).order_by(cur)
    prev = watermark_store.get(source_name)
    if prev is not None:
        try:
            stmt = stmt.where(cur > parse(prev[1]))
        except ValueError as e:
            raise IngestionError(f"{source_name}: stored watermark not parseable") from e

    ci = selected.index(cursor_column)
    out_idx = [selected.index(n) for n in names]
    rows: list[list[str | None]] = []
    last: Any = None
    try:
        with engine.connect() as conn:
            result = conn.execution_options(stream_results=True).execute(stmt)
            while batch := result.fetchmany(batch_size):
                for r in batch:
                    rows.append([_to_str(r[i]) for i in out_idx])
                    last = r[ci]
    except IngestionError:
        raise
    except Exception as e:  # watermark untouched on any failure
        raise IngestionError(f"{table}: extraction failed ({type(e).__name__})") from e

    if last is not None:
        watermark_store.set(source_name, cursor_column, _dump(last))
    wm = _dump(last) if last is not None else (prev[1] if prev else None)
    log.info("sql_extract", source_name=source_name, table=table, rows=len(rows), watermark=wm)
    return RawTable(
        columns=names,
        rows=rows,
        meta={"source_name": source_name, "table": table, "watermark": wm},
    )
