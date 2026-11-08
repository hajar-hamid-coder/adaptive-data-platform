import json
import uuid
from datetime import UTC, date, datetime

import duckdb
import pytest

from adp.bronze.store import LocalStore, S3Store
from adp.bronze.writer import row_hash, write_bronze
from adp.connectors.files import RawTable
from adp.errors import IngestionError

T = RawTable(["id", "name"], [["1", "a"], ["2", None], ["3", "c"]])
D = date(2026, 1, 2)


def _w(table, store, run_id="r1", **kw):
    return write_bronze(table, source="crm", run_id=run_id, store=store, ingestion_date=D, **kw)


def _files(root):
    return sorted(p.name for p in root.rglob("*") if p.is_file())


def test_write_readable_with_tech_columns(tmp_path):
    now = lambda: datetime(2026, 1, 2, 3, 4, tzinfo=UTC)  # noqa: E731
    m = _w(T, LocalStore(tmp_path), now=now)
    assert not m.skipped and m.row_count == 3
    assert m.parquet_path == f"crm/ingestion_date=2026-01-02/{m.batch_hash}.parquet"
    rows = duckdb.sql(
        f"SELECT * FROM read_parquet('{tmp_path / m.parquet_path}', "
        "hive_partitioning=false) ORDER BY id"
    ).fetchall()
    cols = [
        d[0]
        for d in duckdb.sql(
            f"SELECT * FROM read_parquet('{tmp_path / m.parquet_path}', hive_partitioning=false)"
        ).description
    ]
    assert cols == ["id", "name", "_run_id", "_ingested_at", "_source", "_row_hash"]
    assert len(rows) == 3 and rows[1][1] is None
    assert rows[0][2:5] == ("r1", "2026-01-02T03:04:00+00:00", "crm")
    assert rows[0][5] == row_hash(T.columns, ["1", "a"])


def test_manifest_content(tmp_path):
    m = _w(T, LocalStore(tmp_path))
    doc = json.loads((tmp_path / m.parquet_path.replace(".parquet", ".manifest.json")).read_text())
    assert doc["batch_hash"] == m.batch_hash and doc["row_count"] == 3
    assert doc["columns"] == ["id", "name"] and doc["schema"]["id"] == "VARCHAR"
    assert doc["size_bytes"] > 0 and "skipped" not in doc


def test_replay_is_noop_and_file_untouched(tmp_path):
    store = LocalStore(tmp_path)
    m1 = _w(T, store)
    pq = tmp_path / m1.parquet_path
    before = (pq.stat().st_mtime_ns, pq.read_bytes())
    files = _files(tmp_path)
    for rid in ("r1", "r2"):
        m2 = _w(T, store, run_id=rid)
        assert m2.skipped and m2.batch_hash == m1.batch_hash and m2.run_id == "r1"
    assert _files(tmp_path) == files
    assert (pq.stat().st_mtime_ns, pq.read_bytes()) == before


def test_different_batch_new_file(tmp_path):
    store = LocalStore(tmp_path)
    _w(T, store)
    other = RawTable(T.columns, [*T.rows, ["4", "d"]])
    m = _w(other, store)
    assert not m.skipped and len(_files(tmp_path)) == 4


def test_crash_between_parquet_and_manifest(tmp_path):
    class Crashy(LocalStore):
        def put_if_absent(self, key, data):
            if key.endswith(".manifest.json"):
                raise RuntimeError("boom")
            return super().put_if_absent(key, data)

    with pytest.raises(RuntimeError):
        _w(T, Crashy(tmp_path))
    assert _files(tmp_path) and not any(f.endswith(".manifest.json") for f in _files(tmp_path))
    m = _w(T, LocalStore(tmp_path), run_id="r2")
    assert not m.skipped and m.row_count == 3
    assert len(_files(tmp_path)) == 2
    assert _w(T, LocalStore(tmp_path)).skipped


def test_row_hash_stable_and_sensitive():
    h = row_hash(["a", "b"], ["x", None])
    assert h == row_hash(["a", "b"], ["x", None])
    assert h != row_hash(["a", "b"], ["x", ""])
    assert h != row_hash(["a", "b"], ["y", None])
    assert row_hash(["a", "b"], ["xy", "z"]) != row_hash(["a", "b"], ["x", "yz"])


def test_reserved_column_rejected(tmp_path):
    with pytest.raises(IngestionError):
        _w(RawTable(["_run_id"], [["x"]]), LocalStore(tmp_path))


@pytest.mark.slow
def test_minio_roundtrip():
    store = S3Store("bronze")
    try:
        store.exists("probe")
    except IngestionError as e:
        pytest.skip(f"MinIO unreachable: {e}")
    src = f"test_{uuid.uuid4().hex[:8]}"
    m = write_bronze(T, source=src, run_id="r1", store=store, ingestion_date=D)
    keys = [m.parquet_path, m.parquet_path.replace(".parquet", ".manifest.json")]
    try:
        assert not m.skipped and all(store.exists(k) for k in keys)
        again = write_bronze(T, source=src, run_id="r2", store=store, ingestion_date=D)
        assert again.skipped and again.run_id == "r1"
        assert json.loads(store.get(keys[1]))["row_count"] == 3
        assert store.get(keys[0])[:4] == b"PAR1"
    finally:
        for k in keys:
            store.delete(k)
