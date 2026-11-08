"""Schema registry: idempotent versions, semver bump, history, change log (SQLite; PG slow)."""

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text

from adp.connectors.files import RawTable, read_file
from adp.errors import SchemaError
from adp.schema.profile import SchemaFingerprint, profile_table
from adp.schema.registry import (
    SchemaChange,
    SchemaColumn,
    SchemaRegistry,
    classify_bump,
    metadata,
)

DRIFT = Path(__file__).parent.parent / "data" / "drift"


def fp(columns: list[str], rows: list[list[str | None]] | None = None) -> SchemaFingerprint:
    return profile_table(RawTable(columns=columns, rows=rows or [["1"] * len(columns)]))


def drift(v: str) -> SchemaFingerprint:
    return profile_table(read_file(DRIFT / v / "population.csv"))


def col(name: str, typ: str = "integer", nullable: bool = False) -> SchemaColumn:
    return SchemaColumn(name=name, type=typ, nullable=nullable)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = create_engine(f"sqlite:///{tmp_path / 'meta.db'}")
    metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def reg(engine: Engine) -> SchemaRegistry:
    return SchemaRegistry(engine)


# --- validation test of the backlog ---------------------------------------------------------


def test_same_schema_same_version_idempotent(reg: SchemaRegistry, engine: Engine) -> None:
    a = reg.register("hcp", fp(["a", "b"]))
    b = reg.register("hcp", fp(["a", "b"], [["7", "8"], ["9", "10"]]))  # other values
    assert a == b and a.version == 1 and a.semver == "1.0.0"
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM schema_version")).scalar() == 1


def test_change_increments_version(reg: SchemaRegistry) -> None:
    reg.register("hcp", fp(["a"]))
    v2 = reg.register("hcp", fp(["a", "b"]))
    assert v2.version == 2 and v2.semver == "1.1.0"


def test_history_ordered(reg: SchemaRegistry) -> None:
    for cols in (["a"], ["a", "b"], ["b"]):
        reg.register("hcp", fp(cols))
    hist = reg.history("hcp")
    assert [h.version for h in hist] == [1, 2, 3]
    assert [h.semver for h in hist] == ["1.0.0", "1.1.0", "2.0.0"]
    assert [h.created_at for h in hist] == sorted(h.created_at for h in hist)
    assert all(h.created_at.tzinfo is not None for h in hist)


# --- API ------------------------------------------------------------------------------------


def test_get_latest_and_unknown(reg: SchemaRegistry) -> None:
    assert reg.latest("hcp") is None and reg.get("hcp", 1) is None and reg.history("hcp") == []
    v1 = reg.register("hcp", fp(["a"]))
    v2 = reg.register("hcp", fp(["a", "b"]))
    assert reg.get("hcp", 1) == v1 and reg.latest("hcp") == v2 and reg.get("hcp", 3) is None


def test_stored_columns_are_structural(reg: SchemaRegistry) -> None:
    v = reg.register("hcp", fp(["id", "label"], [["1", "x"], ["2", None]]))
    assert v.columns == (col("id"), col("label", "string", True))
    assert v.fingerprint == fp(["id", "label"], [["5", "y"], ["6", None]]).fingerprint


def test_drift_fixtures_semver(reg: SchemaRegistry) -> None:
    v1, v2, v3 = (reg.register("hcp_population", drift(v)) for v in ("v1", "v2", "v3"))
    assert (v1.version, v2.version, v3.version) == (1, 2, 3)
    # v1->v2: add + rename; a rename is seen as drop + add -> breaking.
    # v2->v3: drop taux_activite (types inferred stay integer/float) -> breaking.
    assert (v1.semver, v2.semver, v3.semver) == ("1.0.0", "2.0.0", "3.0.0")


def test_return_to_previous_schema(reg: SchemaRegistry) -> None:
    v1 = reg.register("hcp", fp(["a"]))
    reg.register("hcp", fp(["a", "b"]))
    again = reg.register("hcp", fp(["a"]))
    assert again == v1
    assert [h.version for h in reg.history("hcp")] == [1, 2]
    assert reg.latest("hcp").version == 2  # type: ignore[union-attr]


def test_sources_are_independent(reg: SchemaRegistry) -> None:
    a = reg.register("src_a", fp(["a"]))
    b = reg.register("src_b", fp(["a"]))
    assert a.version == b.version == 1 and a.fingerprint == b.fingerprint
    assert [h.source_name for h in reg.history("src_a")] == ["src_a"]


@pytest.mark.parametrize("name", ["", "Hcp", "1abc", "a-b", "a; DROP TABLE x", "a" * 300])
def test_invalid_source_name(reg: SchemaRegistry, name: str) -> None:
    with pytest.raises(SchemaError, match="source name"):
        reg.register(name, fp(["a"]))
    with pytest.raises(SchemaError, match="source name"):
        reg.history(name)


def test_empty_schema_rejected(reg: SchemaRegistry) -> None:
    with pytest.raises(SchemaError, match="no column"):
        reg.register("hcp", fp([]))


def test_missing_tables_raise_schema_error(tmp_path: Path) -> None:
    eng = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    with pytest.raises(SchemaError, match="schema registry"):
        SchemaRegistry(eng).latest("hcp")
    eng.dispose()


def test_concurrent_register_same_schema(engine: Engine) -> None:
    reg = SchemaRegistry(engine)
    results, errors = [], []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        try:
            results.append(reg.register("hcp", fp(["a", "b"])))
        except Exception as e:  # collected for the assertion below
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert {r.version for r in results} == {1} and len(reg.history("hcp")) == 1


# --- change log -----------------------------------------------------------------------------


def test_record_and_read_changes(reg: SchemaRegistry) -> None:
    reg.register("hcp", fp(["a"]))
    reg.register("hcp", fp(["a", "b"]))
    changes = [
        SchemaChange(change_type="add_column", column_name="b", detail={"type": "integer"}),
        SchemaChange(change_type="other", detail={"note": "x"}),
    ]
    assert reg.record_changes("hcp", 1, 2, changes) == 2
    got = reg.changes("hcp", 2)
    assert [(c.from_version, c.to_version, c.change) for c in got] == [(1, 2, ch) for ch in changes]
    # replaying replaces, never duplicates
    reg.record_changes("hcp", 1, 2, changes[:1])
    assert [c.change for c in reg.changes("hcp", 2)] == changes[:1]
    assert reg.changes("hcp", 1) == []


def test_record_changes_unknown_version(reg: SchemaRegistry) -> None:
    reg.register("hcp", fp(["a"]))
    with pytest.raises(SchemaError, match="unknown version"):
        reg.record_changes("hcp", 1, 2, [SchemaChange(change_type="add_column")])
    with pytest.raises(SchemaError, match="unknown version"):
        reg.record_changes("hcp", 5, 1, [SchemaChange(change_type="add_column")])


# --- classify_bump (pure) -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("prev", "new", "bump"),
    [
        ([col("a")], [col("a"), col("b")], "minor"),
        ([col("a"), col("b")], [col("a")], "major"),
        ([col("a")], [col("b")], "major"),
        ([col("a")], [col("a", "string")], "major"),
        ([col("a")], [col("a", nullable=True)], "patch"),
        ([col("a", nullable=True)], [col("a")], "patch"),
        ([col("a"), col("b")], [col("b"), col("a")], "patch"),
        ([col("a")], [col("a")], "patch"),
    ],
)
def test_classify_bump(prev: list[SchemaColumn], new: list[SchemaColumn], bump: str) -> None:
    assert classify_bump(prev, new) == bump


def test_patch_bump_via_register(reg: SchemaRegistry) -> None:
    reg.register("hcp", fp(["a"], [["1"]]))
    v2 = reg.register("hcp", fp(["a"], [["1"], [None]]))
    assert (v2.version, v2.semver) == (2, "1.0.1")


# --- Postgres (slow, `pg_engine` fixture in conftest.py) ------------------------------------


@pytest.mark.slow
def test_postgres_registry_scenario(pg_engine: Engine) -> None:
    metadata.create_all(pg_engine)
    reg = SchemaRegistry(pg_engine)
    v1, v2, v3 = (reg.register("hcp_population", drift(v)) for v in ("v1", "v2", "v3"))
    assert reg.register("hcp_population", drift("v1")) == v1
    assert [h.semver for h in reg.history("hcp_population")] == ["1.0.0", "2.0.0", "3.0.0"]
    assert reg.latest("hcp_population") == v3 and v2.version == 2

    results: list[int] = []
    barrier = threading.Barrier(6)

    def worker() -> None:
        barrier.wait()
        results.append(reg.register("race", fp(["x"])).version)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [1] * 6 and len(reg.history("race")) == 1
