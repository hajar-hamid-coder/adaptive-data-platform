"""Audit journal (P6-01): idempotence, conflicts, derived run row, constraints, no raw values."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from adp.errors import AuditError
from adp.observability.audit import AuditStore, StepOutcome, audited_step
from adp.schema.registry import metadata

T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> AuditStore:
    eng = create_engine(f"sqlite:///{tmp_path / 'a.db'}")
    metadata.create_all(eng)
    return AuditStore(eng)


def _rec(s: AuditStore, step: str, run: str = "r1", dt: float = 1.0, **kw: object) -> bool:
    out = StepOutcome(**kw)  # type: ignore[arg-type]
    return s.record_step("r1" if run == "r1" else run, "pop", step, out, started_at=T0,
                         finished_at=T0 + timedelta(seconds=dt))  # fmt: skip


def test_replay_is_noop_and_timestamps_ignored(store: AuditStore) -> None:
    assert _rec(store, "bronze", rows_in=10, rows_out=9) is True
    assert _rec(store, "bronze", dt=5.0, rows_in=10, rows_out=9) is False
    assert len(store.steps("r1")) == 1 and len(store.runs()) == 1


def test_different_result_same_key_raises(store: AuditStore) -> None:
    _rec(store, "bronze", rows_in=10)
    with pytest.raises(AuditError, match="différente"):
        _rec(store, "bronze", rows_in=11)
    with pytest.raises(AuditError, match="différente"):
        _rec(store, "bronze", status="failed")
    assert store.steps("r1")[0]["rows_in"] == 10  # history untouched


def test_run_row_derived_from_steps(store: AuditStore) -> None:
    store.start_run("r1", "pop", trigger="sensor", started_at=T0)
    _rec(store, "bronze", rows_in=12, rows_out=10, batch_id="abc")
    _rec(store, "schema_check", schema_version=2, impact="compatible", schema_changes=1)
    _rec(store, "silver", rows_in=10, rows_out=10, schema_version=2)
    run = store.runs()[0]
    assert run["status"] == "success" and run["trigger"] == "sensor" and run["rows_in"] == 12
    _rec(store, "promote", status="quarantined", rows_out=10, rows_quarantined=3,
         quality_score=71.5, batch_id="b9", reasons=["rule:population_non_negative"])  # fmt: skip
    run = store.runs()[0]
    assert run["status"] == "quarantined" and run["quality_score"] == 71.5
    assert (run["rows_in"], run["rows_out"], run["rows_quarantined"]) == (12, 10, 3)
    assert run["schema_version"] == 2 and run["batch_id"] == "b9"
    assert [s["step"] for s in store.steps("r1")] == ["bronze", "schema_check", "silver", "promote"]
    assert store.steps("r1")[3]["reasons"] == ["rule:population_non_negative"]
    assert run["finished_at"] is not None


def test_failure_dominates_and_start_run_idempotent(store: AuditStore) -> None:
    store.start_run("r1", "pop")
    store.start_run("r1", "pop")
    with pytest.raises(AuditError, match="déclencheur"):
        store.start_run("r1", "pop", trigger="cli")
    assert store.runs()[0]["status"] == "running"
    _rec(store, "bronze")
    _rec(store, "schema_check", status="blocked", reasons=["blocking_change"])
    assert store.runs()[0]["status"] == "blocked"
    _rec(store, "silver", status="failed", error_type="TransformError")
    assert (
        store.runs()[0]["status"] == "failed" and store.runs()[0]["error_type"] == "TransformError"
    )


@pytest.mark.parametrize(
    "kw",
    [
        {"error_type": "boom: value 'x@y.z' invalid"},  # a message, not a type name
        {"reasons": ["has space"]},
        {"reasons": ["ok"] * 21},
        {"status": "weird"},
        {"rows_in": -1},
        {"quality_score": 120.0},
        {"impact": "huge"},
    ],
)
def test_invalid_fields_rejected(store: AuditStore, kw: dict[str, object]) -> None:
    with pytest.raises(AuditError):
        _rec(store, "bronze", **kw)
    assert store.runs() == []


def test_invalid_ids_and_step(store: AuditStore) -> None:
    out = StepOutcome()
    for run, src, step in (("bad run", "pop", "bronze"), ("r1", "BAD", "bronze"),
                           ("r1", "pop", "nope")):  # fmt: skip
        with pytest.raises(AuditError):
            store.record_step(run, src, step, out, started_at=T0, finished_at=T0)


def test_constraints_fk_and_unique(store: AuditStore) -> None:
    ins = (
        "INSERT INTO step_audit (run_id, source_name, step, status, started_at, finished_at,"
        " duration_ms, reasons) VALUES ('ghost', 'pop', 'bronze', 'success', '2026-01-01',"
        " '2026-01-01', 0, '')"
    )
    with store.engine.begin() as c:
        c.execute(text("PRAGMA foreign_keys=ON"))
        with pytest.raises(IntegrityError):  # no run_audit parent
            c.execute(text(ins))
    _rec(store, "bronze")
    with store.engine.begin() as c, pytest.raises(IntegrityError):  # bad status (CHECK)
        c.execute(text("UPDATE step_audit SET status='weird'"))


def test_audited_step_records_and_reraises(store: AuditStore) -> None:
    with audited_step(store, run_id="r1", source="pop", step="bronze") as o:
        o.rows_out = 3
    with pytest.raises(ValueError):
        with audited_step(store, run_id="r1", source="pop", step="silver"):
            raise ValueError("secret value 42 inside")
    steps = {s["step"]: s for s in store.steps("r1")}
    assert steps["bronze"]["rows_out"] == 3 and steps["bronze"]["status"] == "success"
    assert steps["silver"]["status"] == "failed" and steps["silver"]["error_type"] == "ValueError"
    assert "secret" not in repr(store.steps("r1")) and steps["silver"]["duration_ms"] >= 0


def test_audit_failure_never_fails_the_run(tmp_path: Path) -> None:
    broken = AuditStore(create_engine(f"sqlite:///{tmp_path / 'empty.db'}"))  # no tables
    with audited_step(broken, run_id="r1", source="pop", step="bronze") as o:
        o.rows_out = 1
    with pytest.raises(KeyError):  # the data error still propagates, the audit stays silent
        with audited_step(broken, run_id="r1", source="pop", step="bronze"):
            raise KeyError("x")
    with audited_step(None, run_id="r1", source="pop", step="bronze"):
        pass
