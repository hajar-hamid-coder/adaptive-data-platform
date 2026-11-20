"""Prometheus metrics (P6-01) on a DEDICATED registry (no global state: isolated tests).

Metrics (names and labels; NEVER a data value, a run_id or an unbounded id as a label):
- adp_runs_total{source,status}             counter   runs by final status (success|failed|
                                                      blocked|quarantined|running)
- adp_step_duration_seconds{step}           histogram step duration (bronze|schema_check|silver|
                                                      promote)
- adp_quality_score{source}                 gauge     batch score (0-100) of the latest run
- adp_schema_changes_total{source,impact}   counter   schema changes seen (compatible|review|
                                                      blocking)
- adp_quarantined_rows_total{source}        counter   rows sent to quarantine
- adp_batches_promoted_total{source}        counter   distinct batches promoted to Gold
- adp_api_requests_total{route,method,status} counter API requests; `route` is the route
                                                      TEMPLATE (/runs/{run_id}), never the raw path

Run-level metrics are computed at scrape time from the audit tables (`AuditCollector`): the
pipeline (Dagster) and the API are separate processes, so an in-process counter incremented by
the pipeline would never reach the scraped endpoint. The database is the shared truth and the
values survive restarts. Only the API request counter lives in the API process.
"""

from collections.abc import Iterable

from prometheus_client import CollectorRegistry, Counter
from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    HistogramMetricFamily,
    Metric,
)
from prometheus_client.registry import Collector
from sqlalchemy import Engine, case, distinct, func, select
from sqlalchemy.exc import SQLAlchemyError

from adp.logging import get_logger
from adp.observability.audit import run_audit, step_audit

log = get_logger(__name__)
BUCKETS_S = (0.05, 0.1, 0.5, 1.0, 5.0, 15.0, 60.0, 300.0)
_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}


class AuditCollector(Collector):
    """Run-level metrics read from `run_audit` / `step_audit` at each scrape."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def collect(self) -> Iterable[Metric]:
        runs = CounterMetricFamily("adp_runs", "Runs by final status", labels=["source", "status"])
        dur = HistogramMetricFamily(
            "adp_step_duration_seconds", "Duration of a pipeline step", labels=["step"]
        )
        score = GaugeMetricFamily(
            "adp_quality_score", "Quality score (0-100) of the latest run", labels=["source"]
        )
        changes = CounterMetricFamily(
            "adp_schema_changes", "Schema changes detected", labels=["source", "impact"]
        )
        quar = CounterMetricFamily(
            "adp_quarantined_rows", "Rows sent to quarantine", labels=["source"]
        )
        promoted = CounterMetricFamily(
            "adp_batches_promoted", "Distinct batches promoted to Gold", labels=["source"]
        )
        r, s = run_audit, step_audit
        try:
            with self._engine.connect() as c:
                for src, st, n in c.execute(
                    select(r.c.source_name, r.c.status, func.count()).group_by(
                        r.c.source_name, r.c.status
                    )
                ):
                    runs.add_metric([src, st], n)
                le = [func.sum(case((s.c.duration_ms <= b * 1000, 1), else_=0)) for b in BUCKETS_S]
                for step, n, total, *cum in c.execute(
                    select(s.c.step, func.count(), func.sum(s.c.duration_ms), *le).group_by(
                        s.c.step
                    )
                ):
                    buckets = [(str(b), int(v)) for b, v in zip(BUCKETS_S, cum, strict=True)]
                    dur.add_metric([step], [*buckets, ("+Inf", n)], int(total) / 1000)
                latest = (
                    select(func.max(r.c.id))
                    .where(r.c.quality_score.is_not(None))
                    .group_by(r.c.source_name)
                )
                for src, q in c.execute(
                    select(r.c.source_name, r.c.quality_score).where(r.c.id.in_(latest))
                ):
                    score.add_metric([src], q)
                for src, imp, n in c.execute(
                    select(s.c.source_name, s.c.impact, func.sum(s.c.schema_changes))
                    .where(s.c.step == "schema_check", s.c.impact.is_not(None))
                    .group_by(s.c.source_name, s.c.impact)
                ):
                    changes.add_metric([src, imp], int(n or 0))
                for src, n in c.execute(
                    select(s.c.source_name, func.sum(s.c.rows_quarantined))
                    .where(s.c.step == "promote")
                    .group_by(s.c.source_name)
                ):
                    quar.add_metric([src], int(n or 0))
                for src, n in c.execute(
                    select(s.c.source_name, func.count(distinct(s.c.batch_id)))
                    .where(s.c.step == "promote", s.c.status == "success")
                    .group_by(s.c.source_name)
                ):
                    promoted.add_metric([src], n)
        except SQLAlchemyError as exc:  # the scrape must still answer
            log.warning("metrics_audit_unavailable", error=type(exc).__name__)
        return [runs, dur, score, changes, quar, promoted]


class Metrics:
    """A registry with the API request counter and, if given, the audit collector."""

    def __init__(self, engine: Engine | None = None) -> None:
        self.registry = CollectorRegistry()
        self._api = Counter(
            "adp_api_requests_total", "API requests", ["route", "method", "status"],
            registry=self.registry,
        )  # fmt: skip
        if engine is not None:
            self.registry.register(AuditCollector(engine))

    def api_request(self, route: str | None, method: str, status: int) -> None:
        """Count a request; `route` is the template (None = no route matched)."""
        self._api.labels(route or "unmatched", method if method in _METHODS else "OTHER",
                         str(status)).inc()  # fmt: skip
