import json
from pathlib import Path

from dagster import DagsterInstance, SkipReason, build_sensor_context, materialize

from adp.bronze.store import LocalStore
from adp.orchestration.definitions import build_definitions
from adp.orchestration.ingestion import BronzeConfig, make_bronze_assets, make_landing_sensor
from adp.sources.spec import SourceSpec

DATA = Path(__file__).parents[1] / "data"


def _spec(name: str = "pop", uri: str | None = None, type_: str = "file") -> SourceSpec:
    return SourceSpec(
        name=name, type=type_, uri=uri or str(DATA / "drift/v1/population.csv"),  # type: ignore[arg-type]
        format="csv", frequency="manual", primary_key=["region"],
    )  # fmt: skip


def _run(spec: SourceSpec, store: LocalStore, **cfg: str):
    return materialize(
        make_bronze_assets({spec.name: spec}), resources={"store": store},
        run_config={"ops": {f"bronze_{spec.name}": {"config": cfg}}} if cfg else None,
    )  # fmt: skip


def _meta(result, name: str):
    ev = result.asset_materializations_for_node(f"bronze_{name}")[0]
    return {k: v.value for k, v in ev.metadata.items()}


def test_materialize_and_replay(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "s")
    r = _run(_spec(), store)
    assert r.success
    m = _meta(r, "pop")
    assert m["rows_ok"] > 0 and m["rows_rejected"] == 0 and m["skipped"] is False
    assert (tmp_path / "s" / m["parquet_path"]).is_file()
    before = store.list()
    assert any(k.endswith("manifest.json") for k in before)
    r2 = _run(_spec(), store)
    assert _meta(r2, "pop")["skipped"] is True
    assert store.list() == before


def test_corrupt_file_goes_to_deadletter(tmp_path: Path) -> None:
    lines = ["id,name"] + [f"{i},n{i}" for i in range(1, 101)]
    lines[10] = "10"
    f = tmp_path / "in.csv"
    f.write_text("\n".join(lines) + "\n")
    store = LocalStore(tmp_path / "s")
    r = _run(_spec("crm", str(f)), store)
    m = _meta(r, "crm")
    assert (m["rows_ok"], m["rows_rejected"]) == (99, 1)
    assert (tmp_path / "s" / m["rejects_path"]).is_file()


def test_non_file_sources_have_no_asset() -> None:
    assert make_bronze_assets({"w": _spec("w", type_="api")}) == []


def test_sensor_skip_then_run_then_no_retrigger(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ADP_LANDING_DIR", str(tmp_path))
    sensor = make_landing_sensor({"pop": _spec()})
    with DagsterInstance.ephemeral() as inst:
        ctx = build_sensor_context(instance=inst)
        assert isinstance(sensor(ctx), SkipReason)
        (tmp_path / "pop").mkdir()
        (tmp_path / "pop" / "a.csv").write_text("region\nx\n")
        req = sensor(ctx)
        assert req.run_key and req.run_config["ops"]["bronze_pop"]["config"]["path"].endswith(
            "a.csv"
        )
        again = build_sensor_context(instance=inst, cursor=ctx.cursor)
        assert isinstance(sensor(again), SkipReason)
        # stable run_key for the same file
        assert sensor(build_sensor_context(instance=inst)).run_key == req.run_key


def test_sensor_path_runs_asset(tmp_path: Path) -> None:
    f = tmp_path / "x.csv"
    f.write_text("id\n1\n2\n")
    r = _run(_spec("pop", "/nonexistent.csv"), LocalStore(tmp_path / "s"), path=str(f))
    assert _meta(r, "pop")["rows_ok"] == 2
    assert BronzeConfig(path="p").path == "p"


def test_definitions_load() -> None:
    d = build_definitions(Path(__file__).parents[2] / "config" / "sources")
    d.validate_loadable(d)
    names = {k.to_user_string() for k in d.resolve_asset_graph().get_all_asset_keys()}
    assert "bronze_demo_population" in names and "bronze_world_bank_gdp" not in names
    assert json.dumps(sorted(names))
