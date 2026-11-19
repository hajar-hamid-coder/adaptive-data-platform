"""Silver -> Gold gate (P4-07): pure decision, promote/quarantine, idempotence, Dagster e2e."""

import json
from pathlib import Path

import pytest
from dagster import AssetKey, materialize

from adp.bronze.store import LocalStore
from adp.mapping.dictionary import load_dictionary
from adp.orchestration.definitions import build_definitions
from adp.orchestration.ingestion import make_bronze_assets
from adp.orchestration.promote_asset import make_promote_assets
from adp.orchestration.promotion_gate import (
    PromotionConfig,
    evaluate_promotion,
    load_promotion_config,
    promote_batch,
)
from adp.orchestration.schema_gate import SchemaRegistryResource, make_schema_check_assets
from adp.orchestration.silver_chain import make_silver_assets
from adp.quality.quarantine import Decision
from adp.quality.rules import QualityReport, RuleResult
from adp.quality.score import BatchScore
from adp.silver.writer import write_silver
from adp.sources.spec import SourceSpec

ROOT = Path(__file__).parents[2]
DRIFT = ROOT / "tests" / "data" / "drift"
CANON = ROOT / "config" / "semantic" / "canonical.yaml"
CFG = PromotionConfig(min_score=80)


def _res(rid: str, severity: str, status: str) -> RuleResult:
    return RuleResult(
        rule_id=rid,
        type="range",
        severity=severity,
        status=status,  # type: ignore[arg-type]
        level="row",
        evaluated=1,
        failed_count=int(status == "failed"),
        failed_rows=[0] if status == "failed" else [],
        detail={},
        message="m",
    )


def _report(*results: RuleResult) -> QualityReport:
    return QualityReport(results=list(results), rows_total=1, failed_row_indices=[],
                         ok=all(r.status != "failed" for r in results))  # fmt: skip


def _score(v: float | None) -> BatchScore:
    return BatchScore(status="scored" if v is not None else "n/a", score=v, completeness=None,
                      validity=None, uniqueness=None, freshness=None, rows_total=1,
                      rows_failed=0, columns=[], config_hash="h")  # fmt: skip


def _dec(level: str = "none") -> Decision:
    return Decision(
        level=level,
        alert=False,
        failed_rate=0.0,  # type: ignore[arg-type]
        batch_reasons=["x"] if level == "batch" else [],
        quarantine_rows=[],
    )


@pytest.mark.parametrize(
    ("report", "score", "decision", "status", "reason"),
    [
        (_report(_res("a", "error", "passed")), 100.0, _dec(), "promoted", None),
        (_report(_res("a", "warn", "failed")), 90.0, _dec(), "promoted", None),
        (_report(_res("a", "error", "failed")), 100.0, _dec("row"), "quarantined", "rule:a"),
        (_report(), 100.0, _dec("batch"), "quarantined", "quarantine:x"),
        (_report(), 79.9, _dec(), "quarantined", "score=79.9<80.0"),
        (_report(), 80.0, _dec(), "promoted", None),
        (_report(), None, _dec(), "quarantined", "score:n/a"),
    ],
)
def test_evaluate_promotion(report, score, decision, status, reason) -> None:
    d = evaluate_promotion(report, _score(score), decision, CFG)
    assert d.status == status
    assert (reason in d.reasons) if reason else d.reasons == []


def test_shipped_config() -> None:
    assert load_promotion_config(ROOT / "config" / "quality" / "promotion.yaml").min_score == 80


GOOD = [["Souss-Massa", 2020, 100, 5.0], ["Fès-Meknès", 2020, 200, 6.0]]


def _silver(store: LocalStore, rows: list[list]) -> str:
    d = load_dictionary(CANON)
    cols = ["region", "year", "population_total", "unemployment_rate"]
    return write_silver((cols, rows), source="pop", schema_version=1, run_id="r1",
                        store=store, dictionary=d).parquet_path  # fmt: skip


def _promote(tmp_path: Path, path: str, run_id: str = "r1"):
    return promote_batch(
        source="pop", silver_path=path, run_id=run_id, dictionary=load_dictionary(CANON),
        silver_store=LocalStore(tmp_path / "silver"), gold_store=LocalStore(tmp_path / "gold"),
        quarantine_store=LocalStore(tmp_path / "q"), report_dir=tmp_path / "rep",
    )  # fmt: skip


def test_conforming_batch_promoted_and_idempotent(tmp_path: Path) -> None:
    path = _silver(LocalStore(tmp_path / "silver"), GOOD)
    r = _promote(tmp_path, path)
    assert r.status == "promoted" and r.score == 100.0 and not r.skipped
    gold = LocalStore(tmp_path / "gold")
    keys = sorted(gold.list("gold_ready/"))
    assert keys == [f"gold_ready/pop/{r.batch_id}/data.parquet",
                    f"gold_ready/pop/{r.batch_id}/manifest.json"]  # fmt: skip
    assert Path(r.report_json).exists() and Path(r.report_html).exists()
    assert json.loads(Path(r.report_json).read_text())["summary"]["status"] == "scored"
    r2 = _promote(tmp_path, path, "r2")
    assert r2.status == "promoted" and r2.skipped
    assert sorted(gold.list("gold_ready/")) == keys


def test_critical_rule_failure_not_promoted(tmp_path: Path) -> None:
    many = [["Souss-Massa", y, 1, 5.0] for y in range(2001, 2010)]
    bad = ["Souss-Massa", 2021, -5, 5.0]  # 1 row of 12: row-level quarantine
    path = _silver(LocalStore(tmp_path / "silver"), [*GOOD, *many, bad])
    r = _promote(tmp_path, path)
    assert r.status == "quarantined" and "rule:population_non_negative" in r.reasons
    assert r.rows_quarantined == 1
    assert LocalStore(tmp_path / "gold").list("gold_ready/") == []
    assert LocalStore(tmp_path / "q").list(f"pop/{r.batch_id}/")
    rep = json.loads(Path(r.report_json).read_text())
    assert rep["quarantine"]["level"] == "row" and Path(r.report_html).exists()
    _promote(tmp_path, path, "r2")  # replay: no error, still nothing in Gold
    assert LocalStore(tmp_path / "gold").list("gold_ready/") == []


# --- Dagster end to end -----------------------------------------------------------------------


def _spec() -> SourceSpec:
    return SourceSpec(
        name="pop", type="file", uri=str(DRIFT / "v1/population.csv"),  # type: ignore[arg-type]
        format="csv", frequency="manual", primary_key=["region", "annee"],
    )  # fmt: skip


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, csv: Path):
    monkeypatch.setenv("ADP_CANONICAL_PATH", str(CANON))
    monkeypatch.setenv("ADP_LEARNED_PATH", str(tmp_path / "learned.yaml"))
    monkeypatch.setenv("ADP_REPORT_DIR", str(tmp_path / "reports"))
    reg = {"pop": _spec()}
    return materialize(
        [*make_bronze_assets(reg), *make_schema_check_assets(reg), *make_silver_assets(reg),
         *make_promote_assets(reg)],
        resources={"store": LocalStore(tmp_path / "bronze"),
                   "silver_store": LocalStore(tmp_path / "silver"),
                   "gold_store": LocalStore(tmp_path / "gold"),
                   "quarantine_store": LocalStore(tmp_path / "q"),
                   "schema_registry": SchemaRegistryResource(url=f"sqlite:///{tmp_path}/r.db")},
        run_config={"ops": {"bronze_pop": {"config": {"path": str(csv)}}}},
        raise_on_error=False,
    )  # fmt: skip


def _meta(r, node: str) -> dict:
    return {k: v.value for k, v in r.asset_materializations_for_node(node)[0].metadata.items()}


def test_dagster_conforming_promoted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = _run(tmp_path, monkeypatch, DRIFT / "v1/population.csv")
    assert r.success
    m = _meta(r, "promote_to_gold_pop")
    assert m["status"] == "promoted" and m["score"] == 100.0
    assert len(LocalStore(tmp_path / "gold").list("gold_ready/")) == 2
    assert Path(m["report_html"]).exists() and Path(m["report_json"]).exists()
    again = _run(tmp_path, monkeypatch, DRIFT / "v1/population.csv")
    assert _meta(again, "promote_to_gold_pop")["skipped"] is True
    assert len(LocalStore(tmp_path / "gold").list("gold_ready/")) == 2


def test_dagster_bad_batch_quarantined_run_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = (DRIFT / "v1/population.csv").read_text(encoding="utf-8").splitlines()
    cells = lines[1].split(",")
    cells[2] = "-5"
    bad = tmp_path / "bad.csv"
    bad.write_text("\n".join([lines[0], ",".join(cells), *lines[2:]]) + "\n", encoding="utf-8")
    r = _run(tmp_path, monkeypatch, bad)
    assert r.success
    m = _meta(r, "promote_to_gold_pop")
    assert m["status"] == "quarantined" and "rule:population_non_negative" in m["reasons"]
    assert LocalStore(tmp_path / "gold").list("gold_ready/") == []
    assert Path(m["report_html"]).exists()


def test_v3_blocked_upstream_no_promotion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run(tmp_path, monkeypatch, DRIFT / "v1/population.csv").success
    r = _run(tmp_path, monkeypatch, DRIFT / "v3/population.csv")
    assert not r.success
    assert not any(e.step_key == "promote_to_gold_pop" for e in r.get_step_success_events())


def test_definitions_wire_promotion() -> None:
    d = build_definitions(ROOT / "config" / "sources")
    g = d.resolve_asset_graph()
    key = AssetKey("promote_to_gold_demo_population")
    assert AssetKey("silver_demo_population") in g.get(key).parent_keys
    assert {"gold_store", "quarantine_store"} <= set(d.resources or {})
