"""Silver chain in Dagster: bronze -> schema_check -> silver (map, clean, dedup, write)."""

from pathlib import Path

import duckdb
import pytest
from dagster import AssetKey, MaterializeResult, MetadataValue, asset, materialize

from adp.bronze.store import LocalStore
from adp.mapping.decision import ReviewQueue
from adp.mapping.dictionary import SemanticDictionary
from adp.mapping.learn import load_dictionary_with_learned, update_learned_file
from adp.mapping.llm import LLMMapper, LocalProvider, MappingProposal, MappingRequest
from adp.mapping.llm import MappingResponse as Resp
from adp.orchestration.definitions import build_definitions
from adp.orchestration.ingestion import make_bronze_assets
from adp.orchestration.schema_gate import SchemaRegistryResource, make_schema_check_assets
from adp.orchestration.silver_chain import make_silver_assets
from adp.sources.spec import SourceSpec

ROOT = Path(__file__).parents[2]
DRIFT = ROOT / "tests" / "data" / "drift"
CANON = ROOT / "config" / "semantic" / "canonical.yaml"
SENTINEL = "Zqxsentinelregion"


def _spec(name: str = "pop", pk: tuple[str, ...] = ("region", "annee")) -> SourceSpec:
    return SourceSpec(
        name=name, type="file", uri=str(DRIFT / "v1/population.csv"),  # type: ignore[arg-type]
        format="csv", frequency="manual", primary_key=list(pk),
    )  # fmt: skip


class CountingProvider:
    """Fake LLM: proposes `region` (0.8, review band) for columns whose candidates include it."""

    name, model, prompt_version = "fake", "fake-1", "map-v1"

    def __init__(self) -> None:
        self.calls = 0

    def propose(self, request: MappingRequest) -> Resp:
        self.calls += 1
        out = tuple(
            MappingProposal(
                source_col=c.name,
                target="region" if "region" in c.candidates else None,
                confidence=0.8,
                rationale="fake",
                model=self.model,
                prompt_version=self.prompt_version,
                source="llm",
            )  # fmt: skip
            for c in request.columns
        )
        return Resp(proposals=out)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    learned = tmp_path / "learned.yaml"
    monkeypatch.setenv("ADP_LEARNED_PATH", str(learned))
    monkeypatch.setenv("ADP_CANONICAL_PATH", str(CANON))
    return {
        "bronze": LocalStore(tmp_path / "bronze"),
        "silver": LocalStore(tmp_path / "silver"),
        "reg": SchemaRegistryResource(url=f"sqlite:///{tmp_path / 'reg.db'}"),
        "learned": learned,
        "root": tmp_path,
    }


def _local(d: SemanticDictionary) -> LLMMapper:
    return LLMMapper(LocalProvider(d), d)


def _run(env, path: Path, spec: SourceSpec | None = None, factory=_local, extra=()):
    spec = spec or _spec()
    reg = {spec.name: spec}
    return materialize(
        [*make_bronze_assets(reg), *make_schema_check_assets(reg),
         *make_silver_assets(reg, mapper_factory=factory), *extra],
        resources={"store": env["bronze"], "silver_store": env["silver"],
                   "schema_registry": env["reg"]},
        run_config={"ops": {f"bronze_{spec.name}": {"config": {"path": str(path)}}}},
        raise_on_error=False,
    )  # fmt: skip


def _meta(result, node: str) -> dict:
    ev = result.asset_materializations_for_node(node)[0]
    return {k: v.value for k, v in ev.metadata.items()}


def _failure(result, node: str):
    ev = next(e for e in result.get_step_failure_events() if e.step_key == node)
    data = ev.event_specific_data.user_failure_data  # type: ignore[union-attr]
    return data, {k: v.value for k, v in data.metadata.items()}


def _ran(result, node: str) -> bool:
    return any(e.step_key == node for e in result.get_step_success_events())


def _silver_files(env, suffix: str = ".parquet") -> list[str]:
    return sorted(k for k in env["silver"].list() if k.endswith(suffix))


def _read(env, key: str) -> tuple[dict[str, str], int]:
    con = duckdb.connect()
    try:
        p = str(env["silver"].root / key)
        types = {
            r[0]: r[1]
            for r in con.execute(
                "DESCRIBE SELECT * FROM read_parquet(?, hive_partitioning = false)", [p]
            ).fetchall()
        }
        n = con.execute(
            "SELECT count(*) FROM read_parquet(?, hive_partitioning = false)", [p]
        ).fetchone()[0]  # type: ignore[index]
    finally:
        con.close()
    return types, n


def test_v1_v2_written_v3_blocked_and_replay_skipped(env) -> None:
    r1 = _run(env, DRIFT / "v1/population.csv")
    assert r1.success
    m1 = _meta(r1, "silver_pop")
    assert m1["schema_version"] == 1 and m1["skipped"] is False
    assert m1["silver_path"].startswith("pop/schema_v1/ingestion_date=")
    types, n = _read(env, m1["silver_path"])
    assert types == {"region": "VARCHAR", "year": "BIGINT", "population_total": "BIGINT",
                     "unemployment_rate": "DOUBLE"}  # fmt: skip
    assert n == 48 and m1["rows_in"] == 48 and m1["rows_written"] == 48
    assert m1["rows_rejected"] == 0 and m1["rows_deduped"] == 0 and m1["pending_review"] == 0
    assert {(x["source"], x["target"]) for x in m1["mapped"]} >= {("annee", "year")}

    r2 = _run(env, DRIFT / "v2/population.csv")
    assert r2.success
    m2 = _meta(r2, "silver_pop")
    assert m2["schema_version"] == 2 and m2["silver_path"].startswith("pop/schema_v2/")
    types2, n2 = _read(env, m2["silver_path"])
    assert {"population_total", "unemployment_rate", "activity_rate"} <= set(types2)
    assert types2["activity_rate"] == "DOUBLE" and n2 == 48

    before = _silver_files(env, "")
    r3 = _run(env, DRIFT / "v3/population.csv")
    assert not r3.success
    assert not _ran(r3, "silver_pop")
    assert r3.get_step_failure_events()[0].step_key == "schema_check_pop"
    assert not [k for k in env["silver"].list() if "schema_v3" in k]
    assert _silver_files(env, "") == before

    r4 = _run(env, DRIFT / "v1/population.csv")  # replay of v1
    assert r4.success
    m4 = _meta(r4, "silver_pop")
    assert m4["skipped"] is True and m4["silver_path"] == m1["silver_path"]
    assert _silver_files(env, "") == before


def _write_csv(path: Path, key_col: str, sentinel: bool = False) -> Path:
    lines = (DRIFT / "v1/population.csv").read_text(encoding="utf-8").splitlines()
    head = lines[0].replace("region", key_col, 1)
    body = lines[1:]
    if sentinel:
        body[0] = body[0].replace(body[0].split(",")[0], SENTINEL, 1)
    path.write_text("\n".join([head, *body]) + "\n", encoding="utf-8")
    return path


def test_mapping_incomplete_then_human_approval_closes_the_loop(env) -> None:
    spec = _spec("popx", ("reg_x", "annee"))
    csv = _write_csv(env["root"] / "popx.csv", "reg_x")
    llm = CountingProvider()
    r1 = _run(env, csv, spec, factory=lambda d: LLMMapper(llm, d))
    assert not r1.success and _ran(r1, "schema_check_popx")
    data, md = _failure(r1, "silver_popx")
    assert md["status"] == "blocked" and md["cause"] == "mapping_incomplete"
    assert "reg_x" in md["missing_key_columns"] and "reg_x" in md["pending_columns"]
    assert "Tanger" not in (data.description or "") + str(md)
    assert _silver_files(env, "") == []
    engine = env["reg"].engine()
    queue = ReviewQueue(engine, load_dictionary_with_learned(CANON, env["learned"]))
    pending = queue.list(status="pending", source_name="popx")
    assert [(p.source_col, p.target) for p in pending] == [("reg_x", "region")]
    assert llm.calls == 1

    again = _run(env, csv, spec, factory=lambda d: LLMMapper(llm, d))  # still waiting: no dup
    assert not again.success and len(queue.list(source_name="popx")) == 1

    queue.approve(pending[0].id, actor="alice", note="ok")
    update_learned_file(CANON, env["learned"], queue.journal())
    llm2 = CountingProvider()
    r2 = _run(env, csv, spec, factory=lambda d: LLMMapper(llm2, d))
    assert r2.success, r2.get_step_failure_events()
    m = _meta(r2, "silver_popx")
    assert llm2.calls == 0
    region = next(x for x in m["mapped"] if x["source"] == "reg_x")
    assert region["target"] == "region" and region["method"] == "synonym"
    assert m["rows_written"] == 48 and m["silver_path"].startswith("popx/schema_v1/")

    # the automatic re-confirmation must not un-learn the human decision
    update_learned_file(CANON, env["learned"], queue.journal())
    assert load_dictionary_with_learned(CANON, env["learned"]).lookup("reg_x") is not None


def test_no_data_value_in_metadata_or_logs(env, capfd: pytest.CaptureFixture[str]) -> None:
    csv = _write_csv(env["root"] / "s.csv", "region", sentinel=True)
    r = _run(env, csv)
    assert r.success
    out, err = capfd.readouterr()
    blob = str(_meta(r, "silver_pop")) + str(_meta(r, "schema_check_pop")) + out + err
    assert SENTINEL not in blob and SENTINEL.lower() not in blob.lower()
    # the sentinel is in Silver (the data), only not in metadata/logs
    con = duckdb.connect()
    p = str(env["silver"].root / _meta(r, "silver_pop")["silver_path"])
    regions = {
        x[0]
        for x in con.execute(
            "SELECT region FROM read_parquet(?, hive_partitioning = false)", [p]
        ).fetchall()
    }
    con.close()
    assert SENTINEL in regions

    spec = _spec("popy", ("reg_x", "annee"))
    r2 = _run(env, _write_csv(env["root"] / "y.csv", "reg_x", sentinel=True), spec)
    data, md = _failure(r2, "silver_popy")
    assert SENTINEL not in (data.description or "") + str(md)


def test_corrupt_learned_file_does_not_stop_the_run(env) -> None:
    env["learned"].write_text(": : [not yaml\n\x00", encoding="utf-8")
    r = _run(env, DRIFT / "v1/population.csv")
    assert r.success and _meta(r, "silver_pop")["rows_written"] == 48


def test_schema_version_comes_from_the_registry(env) -> None:
    r = _run(env, DRIFT / "v2/population.csv")  # first batch of a fresh registry: version 1
    assert r.success
    m = _meta(r, "silver_pop")
    assert m["schema_version"] == _meta(r, "schema_check_pop")["version"] == 1
    assert m["silver_path"].startswith("pop/schema_v1/")


def test_missing_gate_version_fails_explicitly(env) -> None:
    bronze = _run(env, DRIFT / "v1/population.csv")
    path = _meta(bronze, "bronze_pop")["parquet_path"]

    @asset(name="schema_check_pop")
    def fake_check() -> MaterializeResult[None]:
        return MaterializeResult(metadata={"parquet_path": MetadataValue.text(path)})

    spec = _spec()
    r = materialize(
        [fake_check, *make_silver_assets({"pop": spec}, mapper_factory=_local)],
        resources={"store": env["bronze"], "silver_store": env["silver"],
                   "schema_registry": env["reg"]},
        raise_on_error=False,
    )  # fmt: skip
    assert not r.success
    data, _ = _failure(r, "silver_pop")
    assert "version" in (data.description or "")


def test_definitions_have_silver_assets_without_io(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "never_created"
    monkeypatch.setenv("ADP_SILVER_STORE_ROOT", str(root))
    monkeypatch.setenv("ADP_STORE", "local")
    d = build_definitions(ROOT / "config" / "sources")
    graph = d.resolve_asset_graph()
    key = AssetKey("silver_demo_population")
    assert key in graph.get_all_asset_keys()
    assert AssetKey("schema_check_demo_population") in graph.get(key).parent_keys
    assert graph.get(key).group_name == "silver"
    assert "silver_store" in (d.resources or {})
    assert not root.exists()
