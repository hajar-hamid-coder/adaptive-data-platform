"""Scheduled maintenance (P6-06): compaction and retention jobs, STOPPED by default.

Both jobs run in DRY-RUN unless the config explicitly allows applying (`schedule_apply: true` in
config/compaction.yaml / config/retention.yaml) and the kill switches are off. They never raise:
a failure is logged (structured, no data) and the daemon keeps running. Applied runs are
attributed to `dagster-schedule` and journaled like manual ones.
"""

from pathlib import Path
from typing import Any

from dagster import (
    DefaultScheduleStatus,
    JobDefinition,
    OpExecutionContext,
    ScheduleDefinition,
    job,
    op,
)

from adp.bronze.lifecycle import load_retention
from adp.bronze.store import ObjectStore
from adp.logging import get_logger
from adp.optimizer import compaction, retention

log = get_logger(__name__)
KEYS = {"store", "silver_store", "gold_store", "quarantine_store"}
BY = "dagster-schedule"


def compaction_pass(
    bronze: ObjectStore, silver: ObjectStore, gold: ObjectStore, config_path: Path | None = None
) -> dict[str, Any]:
    """One scheduled pass; returns a summary and never raises."""
    try:
        cfg = compaction.load_compaction_config(config_path or Path("config/compaction.yaml"))
        apply = cfg.schedule_apply and cfg.enabled and not compaction.disabled()
        reports = compaction.run_layers(
            {"bronze": bronze, "silver": silver}, [bronze, silver, gold], cfg, apply=apply,
            by=BY, reason="scheduled compaction",
        )  # fmt: skip
        out = {"applied": apply, "groups": sum(len(r.results) for r in reports)}
    except Exception as exc:  # the daemon must survive
        log.error("compaction_job_failed", error=type(exc).__name__)
        return {"error": type(exc).__name__}
    log.info("compaction_job", **out)
    return out


def retention_pass(
    stores: dict[str, ObjectStore], protect_from: list[ObjectStore],
    config_path: Path | None = None,
) -> dict[str, Any]:  # fmt: skip
    try:
        policy = load_retention(config_path or Path("config/retention.yaml"))
        apply = policy.default.schedule_apply and not retention.disabled()
        res = retention.run_retention(stores, protect_from, policy, apply=apply, by=BY,
                                      reason="scheduled retention")  # fmt: skip
        out = {"applied": apply, **{k: v.status for k, v in res.items()}}
    except Exception as exc:
        log.error("retention_job_failed", error=type(exc).__name__)
        return {"error": type(exc).__name__}
    log.info("retention_job", **{k: str(v) for k, v in out.items()})
    return out


@op(required_resource_keys=KEYS)
def compaction_op(context: OpExecutionContext) -> None:
    r = context.resources
    context.log.info(str(compaction_pass(r.store, r.silver_store, r.gold_store)))


@op(required_resource_keys=KEYS)
def retention_op(context: OpExecutionContext) -> None:
    r = context.resources
    stores = {"bronze": r.store, "silver": r.silver_store, "gold": r.gold_store,
              "quarantine": r.quarantine_store}  # fmt: skip
    context.log.info(str(retention_pass(stores, [r.store, r.silver_store, r.gold_store])))


@job(name="compaction_job", description="Compaction Bronze/Silver (dry-run par défaut).")
def compaction_job() -> None:
    compaction_op()


@job(name="retention_job", description="Rétention : fichiers remplacés, gold_opt, quarantaine.")
def retention_job() -> None:
    retention_op()


def make_maintenance() -> tuple[list[JobDefinition], list[ScheduleDefinition]]:
    """Jobs and their schedules (both STOPPED: a human starts them in the Dagster UI)."""
    schedules = [
        ScheduleDefinition(job=compaction_job, cron_schedule="0 3 * * *",
                           default_status=DefaultScheduleStatus.STOPPED),
        ScheduleDefinition(job=retention_job, cron_schedule="30 3 * * *",
                           default_status=DefaultScheduleStatus.STOPPED),
    ]  # fmt: skip
    return [compaction_job, retention_job], schedules
