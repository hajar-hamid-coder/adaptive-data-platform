"""Automated retention deletions (P6-06), on top of `adp.bronze.lifecycle` (policy, archiving).

`lifecycle` keeps ARCHIVING old Bronze batches (move, never delete). This module adds the only
DELETIONS of the platform, each of them verified and bounded:
- superseded: source files replaced by a committed compaction, once `compaction_grace_days` have
  passed AND the target still has the recorded sha256 AND no manifest references the source;
  source manifests stay as tombstones (the compaction record resolves them);
- gold_opt: optimisation artefacts not served (not the fresh artefact of an active
  recommendation) for `gold_opt_days`; manifest removed first (an artefact without manifest is
  ignored by serving), then data; the journal `gold_opt/_journal/` is audit and never purged;
- quarantine: batches whose rows are ALL released/replayed, `quarantine_days` after the last
  event; the record goes first (events alone cannot revive a row).
Gold `gold/` artefacts and `gold_ready/` batches are never candidates.

Guard rails: plan = pure dry-run (default); `apply` needs `by` + `reason`; kill switch
`ADP_RETENTION_DISABLED=1` or `enabled: false`; per-source `hold: true` (legal hold) skips a
source everywhere; more than `max_deletions` objects, or more than `max_fraction` of a scope,
refuses the whole pass unless `confirm_over_cap`; objects are deleted ONE BY ONE through the
store (never a prefix) after a fresh re-plan confirms they are still expired; every deletion is
journaled BEFORE it happens, append-only, as `_retention/journal/<ts>-<id>.json` with the path
and the rule (never any content). A crash between journal and delete is harmless: replay.
"""

import hashlib
import json
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from adp.bronze.lifecycle import RetentionPolicy
from adp.bronze.store import ObjectStore
from adp.errors import OptimizerError
from adp.logging import get_logger
from adp.optimizer.compaction import _records
from adp.quality.quarantine import QuarantineStore

log = get_logger(__name__)

JOURNAL = "_retention/journal/"
OPT = "gold_opt/"


def disabled() -> bool:
    return os.environ.get("ADP_RETENTION_DISABLED", "").strip() == "1"


@dataclass(frozen=True)
class Deletion:
    key: str
    reason: str


@dataclass
class RetentionResult:
    status: str  # dry_run|applied|disabled|refused_cap|refused_fraction
    planned: int
    deleted: int = 0
    skipped: int = 0
    deletions: list[Deletion] = field(default_factory=list)


def _age_ok(then: str, days: int, now: datetime) -> bool:
    return now >= datetime.fromisoformat(then) + timedelta(days=days)


# --- planners (pure: listed state + injected clock) --------------------------------------------
def plan_superseded(
    store: ObjectStore, policy: RetentionPolicy, now: datetime, protected: frozenset[str]
) -> list[Deletion]:
    out = []
    for rec in sorted(_records(store), key=lambda r: r["gid"]):
        if not _age_ok(rec["created_at"], policy.default.compaction_grace_days, now):
            continue
        tgt = rec["target"]
        if not store.exists(tgt) or hashlib.sha256(store.get(tgt)).hexdigest() != rec["sha256"]:
            continue  # target not provably intact: keep the sources
        for s in rec["sources"]:
            if (s.endswith(".parquet") and store.exists(s) and s not in protected
                    and not policy.held(s.split("/", 1)[0])):  # fmt: skip
                out.append(Deletion(s, f"superseded by compaction {rec['gid']}, grace elapsed"))
    return out


def plan_gold_opt(store: ObjectStore, policy: RetentionPolicy, now: datetime) -> list[Deletion]:
    from adp.optimizer.apply import active, current_artefact

    try:
        served = {k for e in active(store).values()
                  if (k := current_artefact(store, e.type, e.target))}  # fmt: skip
    except Exception as exc:  # cannot tell what is served: delete nothing
        log.warning("retention_gold_opt_undetermined", error=type(exc).__name__)
        return []
    out = []
    for k in store.list(OPT):
        if not k.endswith("/manifest.json") or k.startswith(OPT + "_journal/"):
            continue
        base = k.removesuffix("manifest.json")
        data = base + "data.parquet"
        try:
            built = json.loads(store.get(k))["built_at"]
        except (ValueError, KeyError):
            continue
        if data not in served and _age_ok(built, policy.default.gold_opt_days, now):
            why = f"gold_opt artefact unused for {policy.default.gold_opt_days} days"
            out += [Deletion(k, why)] + ([Deletion(data, why)] if store.exists(data) else [])
    return out


def plan_quarantine(store: ObjectStore, policy: RetentionPolicy, now: datetime) -> list[Deletion]:
    qs = QuarantineStore(store)
    out = []
    for k in store.list():
        parts = k.split("/")
        if len(parts) != 3 or parts[2] != "record.json" or policy.held(parts[0]):
            continue
        source, batch = parts[0], parts[1]
        try:
            if qs.state(source, batch).counts["quarantined"]:
                continue
            base = f"{source}/{batch}"
            last = max(
                [json.loads(store.get(k))["created_at"]]
                + [json.loads(store.get(e)).get("at", "") for e in store.list(f"{base}/events/")]
            )  # noqa: E501
        except Exception:  # unreadable batch: keep it
            continue
        if _age_ok(last, policy.default.quarantine_days, now):
            why = f"quarantine batch fully resolved for {policy.default.quarantine_days} days"
            out += [Deletion(k, why)] + [Deletion(e, why) for e in store.list(f"{base}/events/")]
    return out


# --- application -------------------------------------------------------------------------------
def apply_deletions(
    store: ObjectStore, deletions: list[Deletion], replan: Callable[[], list[Deletion]],
    policy: RetentionPolicy, *, apply: bool = False, by: str = "", reason: str = "",
    confirm_over_cap: bool = False, scope_total: int | None = None, now: datetime | None = None,
) -> RetentionResult:  # fmt: skip
    """Dry-run unless `apply`; see the module docstring for the guard rails."""
    res = RetentionResult("dry_run", len(deletions), deletions=list(deletions))
    if not apply:
        return res
    if not (by.strip() and reason.strip()):
        raise OptimizerError("retention --apply : --by et --reason obligatoires")
    if disabled() or not policy.default.enabled:
        log.warning("retention_disabled")
        res.status = "disabled"
        return res
    if len(deletions) > policy.default.max_deletions and not confirm_over_cap:
        res.status = "refused_cap"
        return res
    if (scope_total and len(deletions) / scope_total > policy.default.max_fraction
            and not confirm_over_cap):  # fmt: skip
        res.status = "refused_fraction"
        return res
    fresh = {d.key for d in replan()}  # re-verified right before deleting
    now = now or datetime.now(UTC)
    res.status = "applied"
    for d in deletions:
        if d.key not in fresh or not store.exists(d.key):
            res.skipped += 1
            continue
        entry = {"key": d.key, "rule": d.reason, "by": by, "reason": reason,
                 "at": now.isoformat()}  # fmt: skip
        store.put_if_absent(
            f"{JOURNAL}{now:%Y%m%dT%H%M%S%f}-{uuid.uuid4().hex[:8]}.json",
            json.dumps(entry).encode(),
        )  # journal BEFORE the deletion
        store.delete(d.key)
        res.deleted += 1
    log.info("retention_applied", deleted=res.deleted, skipped=res.skipped)
    return res


def run_retention(
    stores: dict[str, ObjectStore], protect_from: list[ObjectStore], policy: RetentionPolicy,
    *, now: datetime | None = None, apply: bool = False, by: str = "", reason: str = "",
    confirm_over_cap: bool = False,
) -> dict[str, RetentionResult]:  # fmt: skip
    """`stores` keys: bronze/silver (superseded files), gold (gold_opt), quarantine."""
    from adp.optimizer.compaction import referenced_keys

    now = now or datetime.now(UTC)
    out = {}
    for name, st in stores.items():
        if name in ("bronze", "silver"):

            def plan(st: ObjectStore = st) -> list[Deletion]:
                return plan_superseded(st, policy, now, referenced_keys(*protect_from))

            total = None
        elif name == "gold":

            def plan(st: ObjectStore = st) -> list[Deletion]:
                return plan_gold_opt(st, policy, now)

            total = len([k for k in st.list(OPT) if not k.startswith(OPT + "_journal/")])
        elif name == "quarantine":

            def plan(st: ObjectStore = st) -> list[Deletion]:
                return plan_quarantine(st, policy, now)

            total = len(st.list())
        else:
            raise OptimizerError(f"magasin inconnu '{name}'")
        out[name] = apply_deletions(
            st,
            plan(),
            plan,
            policy,
            apply=apply,
            by=by,
            reason=reason,
            confirm_over_cap=confirm_over_cap,
            scope_total=total,
            now=now,
        )
    return out
