"""Immutable Silver writer: typed Parquet versioned by schema, JSON manifest, idempotent.

Layout: `<source>/schema_v<N>/ingestion_date=YYYY-MM-DD/<batch_hash>.parquet` (+ `.manifest.json`),
N = schema registry version supplied by the caller. Same guarantees as Bronze: put-if-absent
(never overwrites), manifest written AFTER the Parquet (= complete batch marker), replay of the
same batch is a no-op (`skipped=True`), a crash between the two files is completed by the replay.

`batch_hash` covers the logical content only: columns sorted by name with their canonical types,
and the typed rows in a deterministic order. It ignores run_id, column order and row order. So
the drift fixture v3, once cleaned, yields the same rows as v1 and therefore the same hash: it is
a skip (same day) or a new copy under another ingestion_date, never a divergent batch. Rows are
written to Parquet in that deterministic order, so the same content gives the same bytes.
Never logs row values.
"""

import hashlib
import json
import math
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb

from adp.bronze.store import ObjectStore
from adp.errors import QualityError, SchemaError
from adp.logging import get_logger
from adp.mapping.dictionary import CanonicalType, SemanticDictionary
from adp.schema.registry import _check_source
from adp.silver.clean import CleanResult

log = get_logger(__name__)

DUCKDB_TYPES: dict[CanonicalType, str] = {
    "integer": "BIGINT", "float": "DOUBLE", "string": "VARCHAR",
    "boolean": "BOOLEAN", "date": "DATE", "datetime": "TIMESTAMP",
}  # fmt: skip
_INT64 = 2**63


@dataclass(frozen=True)
class SilverManifest:
    """Describes one written Silver batch; `skipped` is runtime-only (not persisted)."""

    source: str
    schema_version: int
    run_id: str
    batch_hash: str
    row_count: int
    columns: list[str]
    schema: dict[str, str]
    parquet_path: str
    ingested_at: str
    size_bytes: int
    dictionary_hash: str
    rows_in: int | None = None
    rows_deduped: int | None = None
    rows_rejected: int | None = None
    skipped: bool = False


def _coerce(col: str, ctype: CanonicalType, v: Any) -> Any:
    """Value in its canonical Python type, or QualityError (message without the value)."""
    if v is None:
        return None
    ok = False
    if ctype == "integer":
        ok = type(v) is int and -_INT64 <= v < _INT64
    elif ctype == "float":
        ok = type(v) in (int, float) and math.isfinite(v)
        v = float(v) if ok else v
    elif ctype == "boolean":
        ok = type(v) is bool
    elif ctype == "date":
        ok = type(v) is date
    elif ctype == "datetime":
        ok = isinstance(v, datetime)
        if ok and v.tzinfo is not None:
            v = v.astimezone(UTC).replace(tzinfo=None)
    else:
        ok = isinstance(v, str)
    if not ok:
        raise QualityError(f"column '{col}': value of type {type(v).__name__} is not {ctype}")
    return v


def _enc(v: Any) -> Any:
    return v.isoformat() if isinstance(v, date) else v  # datetime is a date


def _hash_payload(cols: Sequence[str], types: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    order = sorted(range(len(cols)), key=lambda i: cols[i])
    head = [[cols[i], types[i]] for i in order]
    body = sorted(
        json.dumps([_enc(r[i]) for i in order], ensure_ascii=False, separators=(",", ":"))
        for r in rows
    )
    return hashlib.sha256(json.dumps([head, body], ensure_ascii=False).encode()).hexdigest()


def _to_parquet(cols: list[str], ddl_types: list[str], rows: list[list[Any]], dest: Path) -> None:
    ddl = ", ".join(
        f'"{c.replace(chr(34), chr(34) * 2)}" {t}' for c, t in zip(cols, ddl_types, strict=True)
    )
    con = duckdb.connect()
    try:
        con.execute(f"CREATE TABLE t ({ddl})")
        if rows:
            con.executemany(f"INSERT INTO t VALUES ({','.join('?' * len(cols))})", rows)
        con.execute(f"COPY t TO '{dest}' (FORMAT PARQUET)")
    finally:
        con.close()


def write_silver(
    data: CleanResult | tuple[Sequence[str], Sequence[Sequence[Any]]],
    *,
    source: str,
    schema_version: int,
    run_id: str,
    store: ObjectStore,
    dictionary: SemanticDictionary,
    ingestion_date: date | None = None,
    now: Callable[[], datetime] | None = None,
    rows_in: int | None = None,
    rows_deduped: int | None = None,
    rows_rejected: int | None = None,
) -> SilverManifest:
    """Write a typed Silver batch once; replaying the same logical batch is a no-op."""
    _check_source(source)
    if type(schema_version) is not int or schema_version < 1:
        raise SchemaError(f"{source}: schema_version must be an integer >= 1")
    raw_rows: Sequence[Sequence[Any]]
    if isinstance(data, CleanResult):
        columns, raw_rows = list(data.columns), list(data.rows)
        rows_rejected = len(data.rejects) if rows_rejected is None else rows_rejected
    else:
        columns, raw_rows = list(data[0]), data[1]
    types = {f.name: f.type for f in dictionary.fields}
    unknown = [c for c in columns if c not in types]
    if not columns or unknown or len(set(columns)) != len(columns):
        raise SchemaError(f"{source}: empty, duplicate or unknown columns {unknown[:5]}")
    ctypes = [types[c] for c in columns]
    typed: list[list[Any]] = []
    for r in raw_rows:
        if len(r) != len(columns):
            raise QualityError(f"{source}: row width {len(r)} != {len(columns)} columns")
        typed.append([_coerce(c, t, v) for c, t, v in zip(columns, ctypes, r, strict=True)])
    ddl_types = [DUCKDB_TYPES[t] for t in ctypes]
    bh = _hash_payload(columns, ddl_types, typed)
    typed.sort(key=lambda r: json.dumps([_enc(v) for v in r], ensure_ascii=False))

    ts = (now or (lambda: datetime.now(UTC)))()
    day = ingestion_date or ts.date()
    prefix = f"{source}/schema_v{schema_version}/ingestion_date={day.isoformat()}/{bh}"
    pq_key, mf_key = f"{prefix}.parquet", f"{prefix}.manifest.json"
    if store.exists(mf_key):
        log.info("silver_skip", source=source, run_id=run_id, batch_hash=bh)
        return _skipped(store, mf_key)

    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "batch.parquet"
        _to_parquet(columns, ddl_types, typed, local)
        blob = local.read_bytes()
    if not store.put_if_absent(pq_key, blob):  # incomplete earlier attempt: keep its parquet
        blob = store.get(pq_key)
    dhash = hashlib.sha256(dictionary.model_dump_json().encode()).hexdigest()[:16]
    manifest = SilverManifest(
        source=source, schema_version=schema_version, run_id=run_id, batch_hash=bh,
        row_count=len(typed), columns=columns, schema=dict(zip(columns, ddl_types, strict=True)),
        parquet_path=pq_key, ingested_at=ts.astimezone(UTC).isoformat(), size_bytes=len(blob),
        dictionary_hash=dhash, rows_in=rows_in, rows_deduped=rows_deduped,
        rows_rejected=rows_rejected,
    )  # fmt: skip
    body = {k: v for k, v in asdict(manifest).items() if k != "skipped"}
    if not store.put_if_absent(mf_key, json.dumps(body, indent=2).encode()):
        return _skipped(store, mf_key)
    log.info("silver_written", source=source, run_id=run_id, batch_hash=bh,
             schema_version=schema_version, rows=manifest.row_count)  # fmt: skip
    return manifest


def _skipped(store: ObjectStore, mf_key: str) -> SilverManifest:
    return SilverManifest(**json.loads(store.get(mf_key)), skipped=True)
