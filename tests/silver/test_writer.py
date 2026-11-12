"""Silver writer (P3-08): typed Parquet versioned by schema, immutable and idempotent."""

import json
import os
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import pytest
from structlog.testing import capture_logs

from adp.bronze.store import LocalStore, S3Store
from adp.connectors.files import read_file
from adp.errors import IngestionError, QualityError, SchemaError
from adp.mapping.dictionary import load_dictionary
from adp.mapping.rules import map_columns
from adp.mapping.transform import load_field_transforms
from adp.silver.clean import clean_table, load_cleaning
from adp.silver.dedup import dedupe
from adp.silver.writer import write_silver

ROOT = Path(__file__).resolve().parents[2]
SEM = ROOT / "config" / "semantic"
DICT = load_dictionary(SEM / "canonical.yaml")
TRANSFORMS = load_field_transforms(SEM / "transforms.yaml", dictionary=DICT)
RULES = load_cleaning(SEM / "cleaning.yaml", DICT)
DRIFT = ROOT / "tests" / "data" / "drift"
T0 = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)
DAY = date(2026, 11, 8)
COLS = ["region", "year", "unemployment_rate", "period"]
ROWS = [
    ["Fès-Meknès", 2020, 9.5, date(2020, 12, 31)],
    ["Souss-Massa", 2021, None, None],
    ["Béni Mellal", 2019, 12, date(2019, 12, 31)],
]


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(tmp_path / "silver")


def write(store, rows=ROWS, cols=COLS, **kw):
    args = {"source": "hcp", "schema_version": 1, "run_id": "r1", "store": store,
            "dictionary": DICT, "ingestion_date": DAY, "now": lambda: T0} | kw  # fmt: skip
    return write_silver((cols, rows), **args)


def read_back(store, key: str):
    local = Path(os.environ.get("TMPDIR", "/tmp")) / f"{uuid.uuid4().hex}.parquet"
    local.write_bytes(store.get(key))
    try:
        con = duckdb.connect()
        types = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{local}')").fetchall()
        rows = con.execute(f"SELECT * FROM read_parquet('{local}')").fetchall()
        return {t[0]: t[1] for t in types}, rows
    finally:
        local.unlink(missing_ok=True)


def test_typed_parquet_nulls_and_path(store) -> None:
    m = write(store)
    assert m.parquet_path.startswith(f"hcp/schema_v1/ingestion_date=2026-11-08/{m.batch_hash}")
    types, rows = read_back(store, m.parquet_path)
    assert types == {"region": "VARCHAR", "year": "BIGINT", "unemployment_rate": "DOUBLE",
                     "period": "DATE"}  # fmt: skip
    assert m.schema == types
    assert sorted(rows, key=lambda r: r[1]) == [
        ("Béni Mellal", 2019, 12.0, date(2019, 12, 31)),
        ("Fès-Meknès", 2020, 9.5, date(2020, 12, 31)),
        ("Souss-Massa", 2021, None, None),
    ]


def test_manifest_content_and_counts(store) -> None:
    m = write(store, rows_in=10, rows_deduped=4, rows_rejected=3)
    body = json.loads(store.get(m.parquet_path.removesuffix(".parquet") + ".manifest.json"))
    assert body["batch_hash"] == m.batch_hash and body["row_count"] == 3
    assert body["schema_version"] == 1 and body["run_id"] == "r1"
    assert (body["rows_in"], body["rows_deduped"], body["rows_rejected"]) == (10, 4, 3)
    assert body["columns"] == COLS and "skipped" not in body and m.size_bytes > 0


def test_replay_is_noop_and_files_untouched(store) -> None:
    m = write(store)
    snap = {k: (store.get(k), (store.root / k).stat().st_mtime_ns) for k in store.list()}
    again = write(store, run_id="r2", now=lambda: datetime(2026, 11, 9, tzinfo=UTC))
    assert again.skipped and again.batch_hash == m.batch_hash and again.run_id == "r1"
    assert snap == {k: (store.get(k), (store.root / k).stat().st_mtime_ns) for k in store.list()}
    assert len(store.list()) == 2


def test_hash_independent_of_run_row_and_column_order(store) -> None:
    a = write(store)
    perm = [3, 0, 2, 1]
    b = write(store, cols=[COLS[i] for i in perm], rows=[[r[i] for i in perm] for r in ROWS[::-1]])
    assert b.skipped and b.batch_hash == a.batch_hash


def test_other_schema_version_other_path(store) -> None:
    a, b = write(store), write(store, schema_version=2)
    assert not b.skipped and b.batch_hash == a.batch_hash
    assert "schema_v1/" in a.parquet_path and "schema_v2/" in b.parquet_path
    assert len(store.list()) == 4


def test_different_batch_new_file(store) -> None:
    a, b = write(store), write(store, rows=ROWS[:2])
    assert not b.skipped and a.batch_hash != b.batch_hash and len(store.list()) == 4


def test_crash_between_parquet_and_manifest(store) -> None:
    m = write(store)
    mf = m.parquet_path.removesuffix(".parquet") + ".manifest.json"
    (store.root / mf).unlink()  # crash: parquet there, manifest missing
    assert not store.exists(mf)
    before = store.get(m.parquet_path)
    done = write(store)
    assert not done.skipped and store.exists(mf) and store.get(m.parquet_path) == before
    assert len(store.list()) == 2 and write(store).skipped


@pytest.mark.parametrize(
    ("cols", "row"),
    [
        (["year"], ["2020"]),
        (["year"], [True]),
        (["year"], [2.0]),
        (["year"], [2**63]),
        (["unemployment_rate"], ["9,5"]),
        (["unemployment_rate"], [float("nan")]),
        (["unemployment_rate"], [True]),
        (["period"], [datetime(2020, 1, 1)]),
        (["period"], ["2020-01-01"]),
        (["region"], [3]),
    ],
)
def test_invalid_type_refused_before_any_write(store, cols, row) -> None:
    with pytest.raises(QualityError) as e:
        write(store, cols=cols, rows=[row])
    assert str(row[0]) not in str(e.value) or str(row[0]) in cols
    assert store.list() == []


def test_row_width_mismatch(store) -> None:
    with pytest.raises(QualityError):
        write(store, rows=[["a", 1]])


@pytest.mark.parametrize("cols", [["region", "mystery"], [], ["year", "year"]])
def test_bad_columns_refused(store, cols) -> None:
    with pytest.raises(SchemaError):
        write(store, cols=cols, rows=[])
    assert store.list() == []


@pytest.mark.parametrize(
    "kw", [{"schema_version": 0}, {"schema_version": True}, {"source": "../x"}]
)
def test_bad_arguments(store, kw) -> None:
    with pytest.raises(SchemaError):
        write(store, **kw)


def test_tz_aware_datetime_normalized_to_utc() -> None:
    from adp.mapping.dictionary import CanonicalField, SemanticDictionary

    d = SemanticDictionary(fields=(
        CanonicalField(name="ts", type="datetime", description="d"),
        CanonicalField(name="ok", type="boolean", description="b"),
    ))  # fmt: skip
    from datetime import timedelta, timezone

    s = LocalStore(Path(os.environ.get("TMPDIR", "/tmp")) / f"s_{uuid.uuid4().hex}")
    aware = datetime(2020, 1, 1, 12, tzinfo=timezone(timedelta(hours=2)))
    m = write_silver((["ts", "ok"], [[aware, True]]), source="x", schema_version=1, run_id="r",
                     store=s, dictionary=d, now=lambda: T0)  # fmt: skip
    types, rows = read_back(s, m.parquet_path)
    assert types == {"ts": "TIMESTAMP", "ok": "BOOLEAN"}
    assert rows == [(datetime(2020, 1, 1, 10), True)]
    for k in s.list():
        (s.root / k).unlink()


def test_empty_batch_is_valid(store) -> None:
    m = write(store, rows=[])
    assert m.row_count == 0 and read_back(store, m.parquet_path)[1] == []


def test_no_row_values_in_logs(store) -> None:
    with capture_logs() as logs:
        write(store)
        write(store)
    blob = json.dumps(logs, default=str)
    assert "Fès" not in blob and "Souss" not in blob and {"silver_written", "silver_skip"} <= {
        entry["event"] for entry in logs}  # fmt: skip


# --- full chain on the drift fixtures ------------------------------------------------------
def silver_chain(version: str, store, **kw):
    table = read_file(DRIFT / version / "population.csv")
    cands = map_columns(table.columns, DICT)
    mapping = {c: m[0].target for c, m in cands.items() if m}
    res = clean_table(table, mapping, dictionary=DICT, transforms=TRANSFORMS, text_rules=RULES,
                      source=f"drift_{version}", run_id="r", now=lambda: T0)  # fmt: skip
    key = ["region", "year"]
    dd = dedupe(res.columns, res.rows, key_fields=key)
    return write_silver(
        (res.columns, dd.survivors), source="drift", schema_version=1, run_id=f"run_{version}",
        store=store, dictionary=DICT, ingestion_date=DAY, now=lambda: T0,
        rows_in=len(table.rows), rows_deduped=len(dd.survivors),
        rows_rejected=len(res.rejects), **kw,
    )  # fmt: skip


def test_drift_chain_v3_identical_to_v1(store) -> None:
    v1, v3 = silver_chain("v1", store), silver_chain("v3", store)
    assert v1.row_count == 48 and v1.rows_in == 48
    assert v3.batch_hash == v1.batch_hash and v3.skipped  # same typed data = same batch
    assert silver_chain("v2", store).row_count == 48
    types, rows = read_back(store, v1.parquet_path)
    assert types["population_total"] == "BIGINT" and types["unemployment_rate"] == "DOUBLE"
    assert len(rows) == 48


# --- MinIO ---------------------------------------------------------------------------------
@pytest.mark.slow
def test_write_on_minio() -> None:
    store = S3Store("silver")
    try:
        store.exists("probe")
    except IngestionError as e:
        pytest.skip(f"MinIO unreachable: {e}")
    prefix = f"t{uuid.uuid4().hex[:8]}"
    try:
        m = write(store, source=prefix)
        assert not m.skipped and store.exists(m.parquet_path)
        assert write(store, source=prefix).skipped
        assert read_back(store, m.parquet_path)[0]["year"] == "BIGINT"
    except IngestionError as e:
        if "HTTP 404" in str(e) or "NoSuchBucket" in str(e):
            pytest.skip(f"bucket silver indisponible: {e}")
        raise
    finally:
        for k in store.list(prefix):
            store.delete(k)
