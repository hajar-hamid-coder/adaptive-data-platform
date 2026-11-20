"""Audit wired into the Dagster chain (P6-01): steps recorded, failures audited, no run broken."""

from pathlib import Path

import pytest
from dagster import materialize
from sqlalchemy import create_engine

from adp.bronze.store import LocalStore
from adp.observability.audit import AuditStore
from adp.orchestration.ingestion import make_bronze_assets
from adp.orchestration.promote_asset import make_promote_assets
from adp.orchestration.schema_gate import SchemaRegistryResource, make_schema_check_assets
from adp.orchestration.silver_chain import make_silver_assets
from adp.sources.spec import SourceSpec

ROOT = Path(__file__).parents[2]
DRIFT = ROOT / "tests" / "data" / "drift"
SPEC = SourceSpec(
    name="pop", type="file", uri=str(DRIFT / "v1/population.csv"),  # type: ignore[arg-type]
    format="csv", frequency="manual", primary_key=["region", "annee"],
)  # fmt: skip


def _run(tmp_path: Path, mp: pytest.MonkeyPatch, csv: Path):
    mp.setenv("ADP_CANONICAL_PATH", str(ROOT / "config/semantic/canonical.yaml"))
    mp.setenv("ADP_LEARNED_PATH", str(tmp_path / "learned.yaml"))
    mp.setenv("ADP_REPORT_DIR", str(tmp_path / "reports"))
    reg = {"pop": SPEC}
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


def _audit(tmp_path: Path) -> AuditStore:
    return AuditStore(create_engine(f"sqlite:///{tmp_path}/r.db"))


def test_promoted_run_is_audited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = _run(tmp_path, monkeypatch, DRIFT / "v1/population.csv")
    assert r.success
    a = _audit(tmp_path)
    steps = a.steps(r.run_id)
    assert [s["step"] for s in steps] == ["bronze", "schema_check", "silver", "promote"]
    assert {s["status"] for s in steps} == {"success"}
    run = a.runs()[0]
    assert run["run_id"] == r.run_id and run["status"] == "success"
    assert run["quality_score"] == 100.0 and run["schema_version"] == 1
    assert run["rows_in"] and run["rows_out"] and run["batch_id"]
    assert steps[1]["impact"] == "compatible"


def test_quarantined_run_is_audited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lines = (DRIFT / "v1/population.csv").read_text(encoding="utf-8").splitlines()
    cells = lines[1].split(",")
    cells[2] = "-5"
    bad = tmp_path / "bad.csv"
    bad.write_text("\n".join([lines[0], ",".join(cells), *lines[2:]]) + "\n", encoding="utf-8")
    r = _run(tmp_path, monkeypatch, bad)
    assert r.success
    run = _audit(tmp_path).runs()[0]
    assert run["status"] == "quarantined"
    promote = _audit(tmp_path).steps(r.run_id)[-1]
    assert "rule:population_non_negative" in promote["reasons"]


def test_blocked_schema_is_audited_and_run_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _run(tmp_path, monkeypatch, DRIFT / "v1/population.csv").success
    r = _run(tmp_path, monkeypatch, DRIFT / "v3/population.csv")
    assert not r.success  # behaviour unchanged by the audit
    a = _audit(tmp_path)
    steps = {s["step"]: s for s in a.steps(r.run_id)}
    assert steps["schema_check"]["status"] == "blocked"
    assert steps["schema_check"]["reasons"] in (["blocking_change"], ["review_required"])
    assert steps["schema_check"]["schema_changes"] >= 1 and "silver" not in steps
    assert a.runs()[0]["status"] == "blocked"


def test_audit_outage_does_not_fail_data_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(AuditStore, "record_step", lambda *a, **k: 1 / 0)
    assert _run(tmp_path, monkeypatch, DRIFT / "v1/population.csv").success
