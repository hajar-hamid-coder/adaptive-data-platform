"""Schema gate: v1 -> v2 passes with report, v3 stops cleanly (blocked), replays are idempotent."""

from pathlib import Path

import pytest
from dagster import AssetKey, asset, materialize
from sqlalchemy import create_engine

from adp.bronze.store import LocalStore
from adp.orchestration.definitions import build_definitions
from adp.orchestration.ingestion import make_bronze_assets
from adp.orchestration.schema_gate import (
    SchemaRegistryResource,
    make_schema_check_assets,
    read_bronze_table,
    run_schema_gate,
)
from adp.schema.events import EventBus, SchemaChanged
from adp.schema.policy import load_policy
from adp.schema.profile import profile_table
from adp.schema.registry import SchemaRegistry, metadata
from adp.sources.spec import SourceSpec

ROOT = Path(__file__).parents[2]
DATA = ROOT / "tests" / "data" / "drift"
POLICY = load_policy(ROOT / "config" / "evolution_policy.yaml")
PK = ["region", "annee"]
SPEC = SourceSpec(
    name="pop", type="file", uri=str(DATA / "v1/population.csv"),  # type: ignore[arg-type]
    format="csv", frequency="manual", primary_key=PK,
)  # fmt: skip


@pytest.fixture
def env(tmp_path: Path):
    store = LocalStore(tmp_path / "s")
    res = SchemaRegistryResource(url=f"sqlite:///{tmp_path / 'reg.db'}")
    return store, res


def _run(env, path: Path, extra=()):
    store, res = env
    return materialize(
        [*make_bronze_assets({"pop": SPEC}), *make_schema_check_assets({"pop": SPEC}), *extra],
        resources={"store": store, "schema_registry": res},
        run_config={"ops": {"bronze_pop": {"config": {"path": str(path)}}}},
        raise_on_error=False,
    )  # fmt: skip


def _meta(result, name="schema_check_pop"):
    ev = result.asset_materializations_for_node(name)[0]
    return {k: v.value for k, v in ev.metadata.items()}


def _failure(result):
    ev = next(e for e in result.get_step_failure_events() if e.step_key == "schema_check_pop")
    return ev.event_specific_data.user_failure_data  # type: ignore[union-attr]


def test_v1_then_v2_pass_with_report(env) -> None:
    store, _ = env
    r1 = _run(env, DATA / "v1/population.csv")
    assert r1.success and _meta(r1)["status"] == "passed" and _meta(r1)["version"] == 1
    r2 = _run(env, DATA / "v2/population.csv")
    m = _meta(r2)
    assert r2.success and m["status"] == "passed" and m["version"] == 2
    report = store.get(m["report_path"]).decode()
    assert "v1" in report and "v2" in report and "Compatible" in report
    assert "1080551" not in report


def test_v3_blocked_review_required_and_downstream_not_run(env) -> None:
    _run(env, DATA / "v1/population.csv")
    _run(env, DATA / "v2/population.csv")

    @asset(deps=[AssetKey("schema_check_pop")])
    def silver_pop() -> None: ...

    r3 = _run(env, DATA / "v3/population.csv", extra=[silver_pop])
    assert not r3.success
    data = _failure(r3)
    md = {k: v.value for k, v in data.metadata.items()}
    assert md["status"] == "blocked" and md["cause"] == "review_required" and md["version"] == 3
    assert "taux_activite" in md["reasons"] and "format_change" in md["reasons"]
    assert "[" in md["reasons"] and "1 080" not in md["reasons"]
    assert "blocked" in (data.description or "")
    assert not [e for e in r3.get_step_success_events() if e.step_key == "silver_pop"]
    assert r3.get_step_failure_events()[0].step_key == "schema_check_pop"
    # the blocked schema stays registered
    reg = env[1].registry()
    assert reg.latest("pop").version == 3  # type: ignore[union-attr]


def test_blocking_change_when_key_dropped(env, tmp_path: Path) -> None:
    _run(env, DATA / "v1/population.csv")
    f = tmp_path / "nokey.csv"
    f.write_text("region,population,taux_chomage\nA,1,2\nB,3,4\n")
    r = _run(env, f)
    md = {k: v.value for k, v in _failure(r).metadata.items()}
    assert md["status"] == "blocked" and md["cause"] == "blocking_change"
    assert "drop_key_column" in md["reasons"]


def test_replay_idempotent_and_blocked_stays_blocked(env) -> None:
    store, _ = env
    _run(env, DATA / "v1/population.csv")
    bronze_before = [k for k in store.list() if not k.startswith("_schema")]
    r = _run(env, DATA / "v1/population.csv")
    assert r.success and _meta(r)["version"] == 1
    assert [k for k in store.list() if not k.startswith("_schema")] == bronze_before
    _run(env, DATA / "v3/population.csv")
    again = _run(env, DATA / "v3/population.csv")  # replay of a blocked batch
    assert not again.success
    assert env[1].registry().latest("pop").version == 2  # type: ignore[union-attr]
    assert all(k in store.list() for k in bronze_before)


def test_pure_gate_event_once_and_no_values(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "s")
    eng = create_engine(f"sqlite:///{tmp_path / 'r.db'}")
    metadata.create_all(eng)
    reg, bus, events = SchemaRegistry(eng), EventBus(), []
    bus.subscribe(events.append)

    def gate(v: str):
        from adp.connectors.files import read_file

        fp = profile_table(read_file(DATA / v / "population.csv"))
        return run_schema_gate(
            fp, source_name="pop", primary_key=PK, registry=reg, policy=POLICY,
            bus=bus, store=store, run_id="r",
        )  # fmt: skip

    g1, g1b, g2 = gate("v1"), gate("v1"), gate("v2")
    assert (g1.status, g1b.status, g2.status) == ("passed",) * 3
    assert [e.to_version for e in events] == [1, 2] and all(
        isinstance(e, SchemaChanged) for e in events
    )
    assert g1b.report_path == g1.report_path and g2.cause is None
    g3 = gate("v3")
    assert g3.status == "blocked" and g3.reasons
    stored = "".join(store.get(k).decode() for k in store.list("_schema"))
    assert "Tanger" not in stored and "1080551" not in stored


def test_read_bronze_table_drops_technical_columns(env) -> None:
    store, _ = env
    r = _run(env, DATA / "v1/population.csv")
    path = r.asset_materializations_for_node("bronze_pop")[0].metadata["parquet_path"].value
    t = read_bronze_table(store, path)
    assert t.columns == ["region", "annee", "population", "taux_chomage"]


def test_definitions_include_schema_check() -> None:
    d = build_definitions(ROOT / "config" / "sources")
    keys = {k.to_user_string() for a in d.resolve_asset_graph().get_all_asset_keys() for k in [a]}
    assert "schema_check_demo_population" in keys
