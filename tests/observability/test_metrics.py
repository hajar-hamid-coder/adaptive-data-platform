"""Prometheus metrics (P6-01): audit-derived series, dedicated registry, bounded labels."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from prometheus_client import generate_latest
from sqlalchemy import create_engine

from adp.observability.audit import AuditStore, StepOutcome
from adp.observability.metrics import Metrics
from adp.schema.registry import metadata

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _store(tmp_path: Path) -> AuditStore:
    eng = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    metadata.create_all(eng)
    return AuditStore(eng)


def _text(m: Metrics) -> str:
    return generate_latest(m.registry).decode()


def test_audit_derived_series(tmp_path: Path) -> None:
    s = _store(tmp_path)
    for run, secs, score, quar in (("r1", 0.2, 90.0, 0), ("r2", 2.0, 55.0, 4)):
        for step, o in (
            ("schema_check", StepOutcome(schema_changes=2, impact="review")),
            ("promote", StepOutcome(
                status="quarantined" if quar else "success", rows_quarantined=quar,
                quality_score=score, batch_id=f"b-{run}")),
        ):  # fmt: skip
            s.record_step(run, "pop", step, o, started_at=T0,
                          finished_at=T0 + timedelta(seconds=secs))  # fmt: skip
    out = _text(Metrics(s.engine))
    assert 'adp_runs_total{source="pop",status="success"} 1.0' in out
    assert 'adp_runs_total{source="pop",status="quarantined"} 1.0' in out
    assert 'adp_quality_score{source="pop"} 55.0' in out  # latest run
    assert 'adp_schema_changes_total{impact="review",source="pop"} 4.0' in out
    assert 'adp_quarantined_rows_total{source="pop"} 4.0' in out
    assert 'adp_batches_promoted_total{source="pop"} 1.0' in out
    assert 'adp_step_duration_seconds_count{step="promote"} 2.0' in out
    assert 'adp_step_duration_seconds_bucket{le="0.5",step="promote"} 1.0' in out
    assert 'adp_step_duration_seconds_sum{step="promote"} 2.2' in out
    assert "r1" not in out.replace("# HELP", "")  # no run_id as label


def test_registry_is_dedicated_and_isolated(tmp_path: Path) -> None:
    a, b = Metrics(), Metrics()
    a.api_request("/health", "GET", 200)
    assert 'adp_api_requests_total{method="GET",route="/health",status="200"} 1.0' in _text(a)
    assert "adp_api_requests_total{" not in _text(b)
    assert a.registry is not b.registry


def test_api_labels_bounded() -> None:
    m = Metrics()
    m.api_request(None, "GET", 404)
    m.api_request("/x", "BREW", 200)
    out = _text(m)
    assert 'route="unmatched"' in out and 'method="OTHER"' in out


def test_unavailable_audit_does_not_break_scrape(tmp_path: Path) -> None:
    m = Metrics(create_engine(f"sqlite:///{tmp_path / 'none.db'}"))  # no tables
    assert "# TYPE adp_runs_total counter" in _text(m)
