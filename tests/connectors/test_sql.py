"""SQL connector: incremental extraction with watermark (SQLite always, Postgres if reachable)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import (
    Column,
    DateTime,
    Engine,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    insert,
    text,
)
from sqlalchemy.engine import URL

from adp.config import get_settings
from adp.connectors import sql as sql_mod
from adp.connectors.sql import WatermarkStore, extract_incremental
from adp.errors import IngestionError

T0 = datetime(2026, 1, 1, 12, 0, 0)


def _pg_engine() -> Engine:
    s = get_settings()
    url = URL.create(
        "postgresql+psycopg",
        username=s.postgres_user,
        password=s.postgres_password.get_secret_value(),
        host=s.postgres_host,
        port=s.postgres_port,
        database=s.postgres_db,
    )
    return create_engine(url)


@pytest.fixture(
    params=["sqlite", pytest.param("postgres", marks=pytest.mark.slow)],
)
def engine(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Engine]:
    if request.param == "sqlite":
        eng = create_engine(f"sqlite:///{tmp_path / 'src.db'}")
        yield eng
        eng.dispose()
        return
    eng = _pg_engine()
    try:
        with eng.connect():
            pass
    except Exception:
        eng.dispose()
        pytest.skip("Postgres de dev injoignable")
    request.node.cleanup_tables = []
    yield eng
    with eng.begin() as c:
        for t in [*request.node.cleanup_tables, "adp_watermarks_test"]:
            c.execute(text(f'DROP TABLE IF EXISTS "{t}"'))
    eng.dispose()


@pytest.fixture
def table(engine: Engine, request: pytest.FixtureRequest) -> str:
    name = f"t_{uuid.uuid4().hex[:10]}"
    if hasattr(request.node, "cleanup_tables"):
        request.node.cleanup_tables.append(name)
    md = MetaData()
    Table(
        name,
        md,
        Column("id", Integer, primary_key=True),
        Column("label", String(50)),
        Column("updated_at", DateTime),
    )
    md.create_all(engine)
    return name


def _insert(engine: Engine, table: str, ids: range) -> None:
    t = Table(table, MetaData(), autoload_with=engine)
    with engine.begin() as c:
        c.execute(
            insert(t),
            [{"id": i, "label": f"row{i}", "updated_at": T0 + timedelta(minutes=i)} for i in ids],
        )


def _store(engine: Engine) -> WatermarkStore:
    return WatermarkStore(engine, table_name="adp_watermarks_test")


def test_first_read_all_then_nothing_then_five(engine: Engine, table: str) -> None:
    _insert(engine, table, range(1, 4))
    store = _store(engine)
    kw = {"cursor_column": "id", "source_name": "s1", "watermark_store": store}

    first = extract_incremental(engine, table, **kw)
    assert first.columns == ["id", "label", "updated_at"]
    assert [r[0] for r in first.rows] == ["1", "2", "3"]
    assert store.get("s1") == ("id", "3")

    assert extract_incremental(engine, table, **kw).rows == []  # 2nd run: 0 rows

    _insert(engine, table, range(4, 9))
    third = extract_incremental(engine, table, **kw)
    assert [r[0] for r in third.rows] == ["4", "5", "6", "7", "8"]
    assert len(third.rows) == 5
    assert extract_incremental(engine, table, **kw).rows == []


def test_timestamp_cursor_and_column_subset(engine: Engine, table: str) -> None:
    _insert(engine, table, range(1, 4))
    store = _store(engine)
    kw = {
        "cursor_column": "updated_at",
        "source_name": "ts",
        "watermark_store": store,
        "columns": ["id", "updated_at"],
        "batch_size": 2,
    }
    first = extract_incremental(engine, table, **kw)
    assert first.columns == ["id", "updated_at"]
    assert len(first.rows) == 3
    assert extract_incremental(engine, table, **kw).rows == []
    _insert(engine, table, range(4, 6))
    assert len(extract_incremental(engine, table, **kw).rows) == 2


def test_error_mid_read_keeps_watermark(
    engine: Engine, table: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert(engine, table, range(1, 4))
    store = _store(engine)
    kw = {"cursor_column": "id", "source_name": "boom", "watermark_store": store}
    extract_incremental(engine, table, **kw)
    _insert(engine, table, range(4, 10))

    calls = {"n": 0}
    real = sql_mod._to_str

    def flaky(v: object) -> str | None:
        calls["n"] += 1
        if calls["n"] > 8:
            raise RuntimeError("boom")
        return real(v)

    monkeypatch.setattr(sql_mod, "_to_str", flaky)
    with pytest.raises(IngestionError):
        extract_incremental(engine, table, **kw)
    assert store.get("boom") == ("id", "3")

    monkeypatch.setattr(sql_mod, "_to_str", real)
    assert len(extract_incremental(engine, table, **kw).rows) == 6


@pytest.mark.parametrize(
    ("tbl", "cursor", "cols"),
    [
        ("t; DROP TABLE x", "id", None),
        ("{table}", "id; --", None),
        ("{table}", "id", ["id", "label) FROM x --"]),
        ("{table}", "missing", None),
        ("{table}", "id", ["nope"]),
        ("missing_table", "id", None),
        ("", "id", None),
    ],
)
def test_invalid_identifiers(
    engine: Engine, table: str, tbl: str, cursor: str, cols: list[str] | None
) -> None:
    store = _store(engine)
    with pytest.raises(IngestionError):
        extract_incremental(
            engine,
            tbl.format(table=table),
            cursor_column=cursor,
            source_name="bad",
            watermark_store=store,
            columns=cols,
        )
    assert store.get("bad") is None


def test_unreachable_database_error_hides_secret(tmp_path: Path) -> None:
    eng = create_engine(URL.create("postgresql+psycopg", "u", "s3cr3t-pw", "127.0.0.1", 1, "x"))
    with pytest.raises(IngestionError) as ei:
        extract_incremental(
            eng,
            "t",
            cursor_column="id",
            source_name="x",
            watermark_store=WatermarkStore(create_engine(f"sqlite:///{tmp_path / 'w.db'}")),
        )
    assert "s3cr3t-pw" not in str(ei.value)
