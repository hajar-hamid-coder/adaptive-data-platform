"""Non-invasive audit of Dagster assets (P6-01): `@audited_asset(step, source)`.

Wraps the body of an asset: records a `step_audit` row (and the derived `run_audit` row) from
the returned metadata or from the `dagster.Failure` raised. The engine comes from the
`schema_registry` resource (the metadata DB). Any audit problem is logged, never raised.
After each step, `notify_run` (P6-02) evaluates the run for alerts: a no-op without
`ADP_ALERT_WEBHOOK_URL`, and an alerting problem never changes the run outcome.
"""

import functools
import re
from collections.abc import Callable, Mapping
from typing import Any

from dagster import Failure

from adp.logging import get_logger
from adp.observability.alerts import notify_run
from adp.observability.audit import AuditStore, StepOutcome, audited_step

log = get_logger(__name__)
_PROMOTE_STATUS = {"promoted": "success", "quarantined": "quarantined"}
_BLOCKED = "blocked"


def _val(v: Any) -> Any:
    return getattr(v, "value", v)  # MetadataValue -> python value


def _int(v: Any) -> int | None:
    return v if type(v) is int and v >= 0 else None


def _code(v: Any) -> str | None:
    return v if isinstance(v, str) and v else None


def _reason(r: str) -> str:
    """Gate reasons like `score=60<80` carry thresholds: map to the audit's safe code charset."""
    return re.sub(r"[^A-Za-z0-9_:.-]", "_", r)[:64]


def fill_outcome(step: str, md: Mapping[str, Any], out: StepOutcome) -> None:
    """Map asset metadata (counts, ids, codes only) to a step outcome."""
    m = {k: _val(v) for k, v in md.items()}
    status = m.get("status")
    if step == "bronze":
        ok, rej = _int(m.get("rows_ok")), _int(m.get("rows_rejected"))
        out.rows_out = ok
        out.rows_in = None if ok is None or rej is None else ok + rej
        out.batch_id = _code(m.get("batch_hash"))
    elif step == "schema_check":
        out.schema_version = _int(m.get("version"))
        out.schema_changes = _int(m.get("n_changes"))
        out.impact = _code(m.get("overall_impact"))
    elif step == "silver":
        out.schema_version = _int(m.get("schema_version"))
        out.rows_in, out.rows_out = _int(m.get("rows_in")), _int(m.get("rows_written"))
    elif step == "promote":
        out.rows_out, out.rows_quarantined = _int(m.get("rows")), _int(m.get("rows_quarantined"))
        out.batch_id = _code(m.get("batch_id"))
        score = m.get("score")
        out.quality_score = float(score) if isinstance(score, int | float) else None
        reasons = m.get("reasons")
        out.reasons = (
            [_reason(r) for r in reasons if isinstance(r, str) and r]
            if isinstance(reasons, list)
            else []
        )
        status = _PROMOTE_STATUS.get(str(status), status)
    cause = _code(m.get("cause"))
    if cause and step != "promote":
        out.reasons = [cause]
    if status in ("success", "failed", "blocked", "quarantined"):
        out.status = status
    elif step != "promote" and status == "passed":
        out.status = "success"


def _store(context: Any) -> AuditStore | None:
    try:
        return AuditStore(context.resources.schema_registry.engine())
    except Exception as exc:  # no audit rather than no run
        log.warning("audit_unavailable", error=type(exc).__name__)
        return None


def audited_asset(step: str, source: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator for an asset body `fn(context, ...)`; `context.run_id` is the audit run_id."""

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(context: Any, *args: Any, **kwargs: Any) -> Any:
            store = _store(context)
            try:
                with audited_step(store, run_id=context.run_id, source=source,
                                  step=step) as out:  # fmt: skip
                    try:
                        res = fn(context, *args, **kwargs)
                    except Failure as f:
                        if (f.metadata or {}).get("status") is not None:
                            _safe_fill(step, f.metadata, out)
                        raise
                    _safe_fill(step, res.metadata or {}, out)
                    return res
            finally:
                _safe_notify(store, context.run_id, source, step)

        return wrapper

    return deco


def _safe_fill(step: str, md: Mapping[str, Any], out: StepOutcome) -> None:
    try:
        fill_outcome(step, md, out)
    except Exception as exc:
        log.warning("audit_outcome_unreadable", step=step, error=type(exc).__name__)


def _safe_notify(store: AuditStore | None, run_id: str, source: str, step: str) -> None:
    if store is None:
        return
    try:
        notify_run(store, run_id, source, step=step)
    except Exception as exc:  # notify_run never raises; belt and braces
        log.warning("alert_hook_failed", step=step, error=type(exc).__name__)
