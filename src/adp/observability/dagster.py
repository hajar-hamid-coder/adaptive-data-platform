"""Non-invasive audit of Dagster assets (P6-01): `@audited_asset(step, source)`.

Wraps the body of an asset: records a `step_audit` row (and the derived `run_audit` row) from
the returned metadata or from the `dagster.Failure` raised. The engine comes from the
`schema_registry` resource (the metadata DB). Any audit problem is logged, never raised.
"""

import functools
from collections.abc import Callable, Mapping
from typing import Any

from dagster import Failure

from adp.logging import get_logger
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
            [r for r in reasons if isinstance(r, str)] if isinstance(reasons, list) else []
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
            with audited_step(_store(context), run_id=context.run_id, source=source,
                              step=step) as out:  # fmt: skip
                try:
                    res = fn(context, *args, **kwargs)
                except Failure as f:
                    if (f.metadata or {}).get("status") is not None:
                        _safe_fill(step, f.metadata, out)
                    raise
                _safe_fill(step, res.metadata or {}, out)
                return res

        return wrapper

    return deco


def _safe_fill(step: str, md: Mapping[str, Any], out: StepOutcome) -> None:
    try:
        fill_outcome(step, md, out)
    except Exception as exc:
        log.warning("audit_outcome_unreadable", step=step, error=type(exc).__name__)
