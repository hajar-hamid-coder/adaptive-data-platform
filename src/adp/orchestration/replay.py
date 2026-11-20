"""Smart auto-replay (P6-03): after a fix, replay a blocked batch, idempotently and bounded.

Flow (`run_replays`): `find_blocked` (audit) -> `detect_fixes` (pure) -> per candidate:
kill switch -> key state (terminal / in flight / backoff / budget) -> global window ->
atomic reservation -> kill switch AGAIN -> `replay_batch` -> outcome -> alerts.

Guard rails ("self-healing without runaway"):
- Recoverable blocks only: the latest non-replay run of a Bronze batch (failed REPLAY runs are
  skipped: their retries are tracked here) must be `blocked` by `mapping_incomplete` (silver)
  or `review_required` (schema_check). A technical failure, a `blocking_change`, a run still
  `running`, an unknown cause is NEVER replayed. A later `success`/`quarantined` run closes it.
- Fixes (`detect_fixes`, pure, deterministic): a HUMAN `approve` of the source journaled AFTER
  the block (never a system `auto_apply`, a reject or the passing of time); a human acceptance
  of the blocked schema version after the block; a change of the versioned config file of the
  cause (`canonical.yaml` for mapping, `evolution_policy.yaml` for the gate) against the
  fingerprint recorded when the block was first observed (`replay_baseline`). One candidate per
  batch (human fix first). fix_id = sha256 of the fix (decision id, version + acceptance time,
  file + new fingerprint)[:16].
- Idempotence key = (source, bronze_batch, fix_id). A replay blocked again (same or another
  cause) is TERMINAL for that fix (`failed`, reason `blocked:<cause>`) and becomes the new block:
  older fixes no longer count, so the same cause never loops and burns no extra budget.
  A success closes the batch (it is no longer blocked: nothing is replayed again).
- Budget: `max_attempts` (H-02: 3) counted attempts per key, only TECHNICAL errors are retried,
  with exponential backoff + bounded jitter (pure `backoff_delay`, injectable clock and random).
  Last failure -> `exhausted` + one `replay_exhausted` alert (conditional update + AlertStore
  dedup), then nothing until a new human fix (new fix_id) or `adp replay reset` (journaled row
  `reset` with actor/note: attempts before it no longer count). Global soft cap
  `max_per_window` per `window_minutes` and `max_per_cycle` per call.
- Kill switch: `ADP_REPLAY_DISABLED=1` (wins), then `enabled` global and per source of
  config/replay.yaml (missing file = disabled, invalid file = refused). Re-read for EVERY
  candidate before the reservation AND right before execution (a cut in between turns the
  reserved attempt into `skipped`, which does not consume budget).
- Concurrency: the reservation is one INSERT on UNIQUE(source, batch, fix_id, attempt_no); the
  loser skips. `planned -> running` is a conditional UPDATE. A `planned`/`running` attempt older
  than `running_timeout_minutes` (crash) becomes `failed/stale_timeout`: counted, not blocking.
- Execution reuses the chain on the ORIGINAL Bronze batch (no new ingestion): learn (approved
  synonyms -> learned file, idempotent) -> `run_schema_gate` -> `run_silver_chain` ->
  `promote_batch`; Silver/Gold writes are already put_if_absent/hash-idempotent. Each step is
  audited (`trigger=replay`, deterministic run_id per attempt) and `notify_run` is called
  (recovery closes the open alerts of the source).
- Privacy: rows, logs and alerts hold source names, Bronze keys, hashes, counts and reason codes
  `[A-Za-z0-9_:.-]{1,64}`; exceptions are reduced to their TYPE name.
"""

import hashlib
import os
import random
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Engine,
    Index,
    Integer,
    String,
    Table,
    UniqueConstraint,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from adp.bronze.store import ObjectStore
from adp.errors import ConfigError, ReplayError, TransformError
from adp.logging import get_logger
from adp.mapping.decision import SYSTEM, LogEntry, ReviewQueue
from adp.mapping.learn import load_dictionary_with_learned, update_learned_file
from adp.observability.alerts import (
    Alert,
    AlertSender,
    AlertStore,
    dispatch,
    load_alert_policy,
    notify_run,
    sender_from_env,
)
from adp.observability.audit import AuditStore, audited_step, run_audit, step_audit
from adp.schema.registry import SchemaRegistry, _check_source, metadata
from adp.sources.spec import SourceSpec

log = get_logger(__name__)
DEFAULT_CONFIG = "config/replay.yaml"
KILL_ENV = "ADP_REPLAY_DISABLED"
TRIGGER = "replay"
RECOVERABLE = {"mapping_incomplete": "silver", "review_required": "schema_check"}
CONFIG_OF_CAUSE = {"mapping_incomplete": "canonical", "review_required": "evolution_policy"}
STATUSES = ("planned", "running", "succeeded", "failed", "skipped", "exhausted", "reset")
COUNTED = ("planned", "running", "succeeded", "failed", "exhausted")
_CODE = re.compile(r"^[A-Za-z0-9_:.-]{1,64}$")
_NOTE_MAX = 500

Status = Literal["planned", "running", "succeeded", "failed", "skipped", "exhausted", "reset"]

replay_attempt = Table(
    "replay_attempt",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source_name", String(255), nullable=False),
    Column("bronze_batch", String(512), nullable=False),
    Column("fix_id", String(32), nullable=False),
    Column("attempt_no", Integer, nullable=False),
    Column("status", String(16), nullable=False),
    Column("reason_code", String(64), nullable=False),
    Column("run_id", String(64), nullable=True),
    Column("scheduled_at", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    Column("next_at", DateTime(timezone=True), nullable=True),
    Column("actor", String(128), nullable=True),
    Column("note", String(_NOTE_MAX), nullable=True),
    UniqueConstraint(
        "source_name", "bronze_batch", "fix_id", "attempt_no", name="uq_replay_attempt_key"
    ),  # fmt: skip
    CheckConstraint(
        f"status IN ({', '.join(repr(s) for s in STATUSES)})", name="ck_replay_attempt_status"
    ),  # fmt: skip
    Index("ix_replay_attempt_batch", "source_name", "bronze_batch"),
)

replay_baseline = Table(
    "replay_baseline",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source_name", String(255), nullable=False),
    Column("bronze_batch", String(512), nullable=False),
    Column("block_run_id", String(64), nullable=False),
    Column("config_fp", String(64), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("source_name", "bronze_batch", "block_run_id", name="uq_replay_baseline"),
)


# --- policy ----------------------------------------------------------------------------------
class BackoffConfig(BaseModel):
    """Delay before retry n = min(max, cooldown * factor^(n-1)) * (1 +- jitter), capped."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cooldown_seconds: float = Field(default=60, gt=0, le=86400)
    factor: float = Field(default=2, ge=1, le=10)
    max_seconds: float = Field(default=3600, gt=0, le=7 * 86400)
    jitter: float = Field(default=0.2, ge=0, le=0.5)


class SourceReplay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True


class ReplayPolicy(BaseModel):
    """config/replay.yaml. Defaults are SAFE: disabled unless the file says otherwise."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    dry_run: bool = False
    max_attempts: int = Field(default=3, ge=1, le=10)  # H-02, per (source, batch, fix_id)
    window_minutes: int = Field(default=60, ge=1, le=1440)
    max_per_window: int = Field(default=10, ge=0, le=1000)
    max_per_cycle: int = Field(default=2, ge=1, le=100)
    running_timeout_minutes: int = Field(default=30, ge=1, le=1440)
    backoff: BackoffConfig = BackoffConfig()
    sources: dict[str, SourceReplay] = Field(default_factory=dict)


def load_replay_policy(path: Path) -> ReplayPolicy:
    """Policy from YAML; a missing file means disabled. Raises ConfigError if invalid."""
    if not path.exists():
        return ReplayPolicy()
    try:
        return ReplayPolicy.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    except (ValidationError, yaml.YAMLError) as exc:
        n = exc.error_count() if isinstance(exc, ValidationError) else 1
        raise ConfigError(
            f"{path.name} : configuration de rejeu invalide ({n} erreur(s))"
        ) from None


def _policy_from_env() -> ReplayPolicy:
    return load_replay_policy(Path(os.environ.get("ADP_REPLAY_CONFIG", DEFAULT_CONFIG)))


def kill_switch(
    source: str, policy: ReplayPolicy, env: Mapping[str, str] | None = None
) -> str | None:
    """Reason code if replay is forbidden for `source`, else None (env var wins)."""
    env = os.environ if env is None else env
    if env.get(KILL_ENV, "").strip().lower() in ("1", "true", "yes", "on"):
        return "env_disabled"
    if not policy.enabled:
        return "global_disabled"
    src = policy.sources.get(source)
    if src is not None and not src.enabled:
        return "source_disabled"
    return None


def backoff_delay(attempt: int, cfg: BackoffConfig, rand: float) -> float:
    """Seconds to wait after the `attempt`-th failure; `rand` in [0, 1] (pure, deterministic)."""
    base = min(cfg.max_seconds, cfg.cooldown_seconds * cfg.factor ** max(0, attempt - 1))
    jittered = base * (1 - cfg.jitter + 2 * cfg.jitter * min(1.0, max(0.0, rand)))
    return max(0.0, min(cfg.max_seconds, jittered))


# --- detection (pure) ------------------------------------------------------------------------
@dataclass(frozen=True)
class BlockedBatch:
    """A Bronze batch whose latest decisive run is blocked by a recoverable cause."""

    source: str
    bronze_batch: str
    cause: str
    blocked_at: datetime
    block_run_id: str
    schema_version: int | None = None
    schema_accepted_at: datetime | None = None
    config_baseline: str | None = None


@dataclass(frozen=True)
class ReplayCandidate:
    source: str
    bronze_batch: str
    reason: str
    fix_id: str


def _hash(*parts: object) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:16]


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # SQLite drops the time zone


def detect_fixes(
    blocked: Sequence[BlockedBatch],
    decision_log: Sequence[LogEntry],
    config_fingerprints: Mapping[str, str],
) -> list[ReplayCandidate]:
    """Candidates whose block has been fixed since (see module docstring). Pure."""
    out = []
    for b in blocked:
        fix: tuple[str, str] | None = None
        if b.cause == "mapping_incomplete":
            human = [
                e for e in decision_log
                if e.source_name == b.source and e.action == "approve" and e.actor != SYSTEM
                and _utc(e.decided_at) > b.blocked_at
            ]  # fmt: skip
            if human:
                fix = ("mapping_approved", _hash("mapping_approved", b.source,
                                                 max(e.id for e in human)))  # fmt: skip
        elif b.cause == "review_required" and b.schema_accepted_at is not None:
            if b.schema_accepted_at > b.blocked_at:
                fix = ("schema_accepted", _hash("schema_accepted", b.source, b.schema_version,
                                                b.schema_accepted_at.isoformat()))  # fmt: skip
        name = CONFIG_OF_CAUSE.get(b.cause)
        current = config_fingerprints.get(name or "")
        if fix is None and name and b.config_baseline and current and current != b.config_baseline:
            fix = (f"config_changed:{name}", _hash("config_changed", name, current))
        if fix is not None:
            out.append(ReplayCandidate(b.source, b.bronze_batch, fix[0], fix[1]))
    return sorted(out, key=lambda c: (c.source, c.bronze_batch))


# --- context and discovery -------------------------------------------------------------------
MapperFactory = Callable[[Any], Any]


@dataclass(frozen=True)
class ReplayContext:
    """Everything a replay needs (stores, metadata DB, config paths)."""

    engine: Engine
    sources: Mapping[str, SourceSpec]
    bronze_store: ObjectStore
    silver_store: ObjectStore
    gold_store: ObjectStore | None
    quarantine_store: ObjectStore | None
    report_dir: Path
    canonical_path: Path
    learned_path: Path
    policy_path: Path
    quality_dir: Path = Path("config/quality")
    semantic_dir: Path = Path("config/semantic")
    mapper_factory: MapperFactory | None = None
    lookback_runs: int = 2000

    @classmethod
    def from_env(cls, sources: Mapping[str, SourceSpec], db: str | None = None) -> Self:
        """Same stores and paths as the Dagster definitions (environment variables)."""
        from adp.mapping.learn import DEFAULT_LEARNED_PATH
        from adp.orchestration.ingestion import build_store
        from adp.orchestration.promote_asset import build_gold_store, build_quarantine_store
        from adp.orchestration.schema_gate import DEFAULT_POLICY
        from adp.orchestration.silver_chain import SEMANTIC, build_silver_store
        from adp.schema.registry import make_engine

        return cls(
            engine=make_engine(db), sources=sources, bronze_store=build_store(),
            silver_store=build_silver_store(), gold_store=build_gold_store(),
            quarantine_store=build_quarantine_store(),
            report_dir=Path(os.environ.get("ADP_REPORT_DIR", "data/reports")),
            canonical_path=Path(os.environ.get("ADP_CANONICAL_PATH",
                                               str(SEMANTIC / "canonical.yaml"))),
            learned_path=Path(os.environ.get("ADP_LEARNED_PATH", str(DEFAULT_LEARNED_PATH))),
            policy_path=Path(os.environ.get("ADP_POLICY_PATH", DEFAULT_POLICY)),
        )  # fmt: skip


def config_fingerprints(ctx: ReplayContext) -> dict[str, str]:
    """sha256[:16] of the versioned config files a fix can come from ('missing' if absent)."""
    out = {}
    for name, path in (("canonical", ctx.canonical_path), ("evolution_policy", ctx.policy_path)):
        out[name] = hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path.exists() else (
            "missing")  # fmt: skip
    return out


def find_blocked(ctx: ReplayContext) -> list[BlockedBatch]:
    """Recoverable blocked batches from the audit journal (baselines not filled here)."""
    r, s = run_audit, step_audit
    try:
        with ctx.engine.connect() as c:
            runs = list(c.execute(
                select(r.c.run_id, r.c.source_name, r.c.trigger, r.c.status, r.c.finished_at,
                       r.c.started_at)
                .order_by(r.c.started_at.desc(), r.c.id.desc()).limit(ctx.lookback_runs)
            ))  # fmt: skip
            ids = {x.run_id for x in runs}
            steps = [x for x in c.execute(select(s)) if x.run_id in ids]
    except SQLAlchemyError as exc:
        raise ReplayError(f"lecture de l'audit impossible ({type(exc).__name__})") from None
    by_run: dict[tuple[str, str], list[Any]] = {}
    for st in steps:
        by_run.setdefault((st.run_id, st.source_name), []).append(st)
    decided: set[tuple[str, str]] = set()
    paths: dict[str, dict[str, str]] = {}
    registry = SchemaRegistry(ctx.engine)
    out = []
    for run in runs:  # newest first
        mine = by_run.get((run.run_id, run.source_name), [])
        batch = next((x.batch_id for x in mine
                      if x.step in ("bronze", "schema_check") and x.batch_id), None)  # fmt: skip
        if batch is None or run.source_name not in ctx.sources:
            continue
        key = (run.source_name, batch)
        if key in decided or (run.trigger == TRIGGER and run.status == "failed"):
            continue
        decided.add(key)
        if run.status != "blocked":
            continue  # success/quarantined = resolved; failed/running = not recoverable
        step = next((x for x in mine if x.status == "blocked"), None)
        cause = (step.reasons or "").split()[0] if step is not None and step.reasons else ""
        if step is None or RECOVERABLE.get(cause) != step.step:
            continue
        if run.source_name not in paths:
            paths[run.source_name] = {
                k.rsplit("/", 1)[-1].removesuffix(".parquet"): k
                for k in ctx.bronze_store.list(f"{run.source_name}/") if k.endswith(".parquet")
            }  # fmt: skip
        path = paths[run.source_name].get(batch)
        if path is None:
            log.warning("replay_bronze_missing", source=run.source_name, batch_id=batch)
            continue
        accepted = next((v.accepted_at for v in registry.history(run.source_name)
                         if v.version == step.schema_version), None)  # fmt: skip
        out.append(BlockedBatch(
            source=run.source_name, bronze_batch=path, cause=cause,
            blocked_at=_utc(run.finished_at or run.started_at), block_run_id=run.run_id,
            schema_version=step.schema_version,
            schema_accepted_at=_utc(accepted) if accepted else None,
        ))  # fmt: skip
    return sorted(out, key=lambda b: (b.source, b.bronze_batch))


# --- store -----------------------------------------------------------------------------------
@dataclass
class KeyState:
    """Attempts of one (source, batch, fix_id) since the latest reset of the batch."""

    counted: int = 0
    terminal: str | None = None
    in_flight: bool = False
    next_at: datetime | None = None
    next_no: int = 1
    last_failed_id: int | None = None


def _code(value: str) -> str:
    if not isinstance(value, str) or not _CODE.fullmatch(value):
        raise ReplayError("code de raison invalide ([A-Za-z0-9_:.-]{1,64} uniquement)")
    return value


class ReplayStore:
    """Persistent replay state (tables owned by Alembic 0007). Raises ReplayError."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def _exec(self, fn: Callable[[Any], Any]) -> Any:
        try:
            with self._engine.begin() as c:
                return fn(c)
        except SQLAlchemyError as exc:
            raise ReplayError(f"état de rejeu indisponible ({type(exc).__name__})") from None

    def ensure_baseline(
        self, source: str, batch: str, block_run_id: str, fp: str, now: datetime, write: bool
    ) -> str | None:
        """Config fingerprint seen at the first observation of this block (insert if absent)."""
        t = replay_baseline
        mine = (t.c.source_name == source, t.c.bronze_batch == batch,
                t.c.block_run_id == block_run_id)  # fmt: skip
        if write:
            try:
                with self._engine.begin() as c:
                    c.execute(insert(t).values(source_name=source, bronze_batch=batch,
                                               block_run_id=block_run_id, config_fp=fp,
                                               observed_at=now))  # fmt: skip
            except IntegrityError:
                pass  # already observed (or concurrently): keep the first fingerprint
            except SQLAlchemyError as exc:
                raise ReplayError(f"état de rejeu indisponible ({type(exc).__name__})") from None
        return self._exec(lambda c: c.execute(select(t.c.config_fp).where(*mine)).scalar())  # type: ignore[no-any-return]

    def state(self, source: str, batch: str, fix_id: str) -> KeyState:
        t = replay_attempt

        def read(c: Any) -> list[Any]:
            reset = c.execute(select(func.max(t.c.id)).where(
                t.c.source_name == source, t.c.bronze_batch == batch, t.c.status == "reset"
            )).scalar() or 0  # fmt: skip
            rows = list(c.execute(select(t).where(
                t.c.source_name == source, t.c.bronze_batch == batch, t.c.fix_id == fix_id
            ).order_by(t.c.id)))  # fmt: skip
            return [reset, rows]

        reset, rows = self._exec(read)
        st = KeyState(next_no=max((x.attempt_no for x in rows), default=0) + 1)
        for x in (x for x in rows if x.id > reset):
            st.counted += x.status in COUNTED
            st.in_flight |= x.status in ("planned", "running")
            if x.status in ("succeeded", "exhausted") or (
                x.status == "failed" and x.reason_code.startswith("blocked:")
            ):
                st.terminal = x.status
            if x.status == "failed":
                st.next_at = _utc(x.next_at) if x.next_at else None
                st.last_failed_id = x.id
        return st

    def reserve(
        self, source: str, batch: str, fix_id: str, attempt_no: int, now: datetime, run_id: str
    ) -> bool:
        """Atomic reservation (`planned`): True for exactly one concurrent caller."""
        _check_source(source)
        try:
            with self._engine.begin() as c:
                c.execute(insert(replay_attempt).values(
                    source_name=source, bronze_batch=batch, fix_id=fix_id, attempt_no=attempt_no,
                    status="planned", reason_code="reserved", run_id=run_id, scheduled_at=now,
                ))  # fmt: skip
            return True
        except IntegrityError:
            return False
        except SQLAlchemyError as exc:
            raise ReplayError(f"état de rejeu indisponible ({type(exc).__name__})") from None

    def _key(self, source: str, batch: str, fix_id: str, attempt_no: int) -> tuple[Any, ...]:
        t = replay_attempt
        return (t.c.source_name == source, t.c.bronze_batch == batch, t.c.fix_id == fix_id,
                t.c.attempt_no == attempt_no)  # fmt: skip

    def start(self, source: str, batch: str, fix_id: str, attempt_no: int, now: datetime) -> bool:
        """planned -> running (conditional): False if the attempt is no longer planned."""
        t, key = replay_attempt, self._key(source, batch, fix_id, attempt_no)
        res = self._exec(lambda c: c.execute(
            update(t).where(*key, t.c.status == "planned").values(status="running",
                                                                  started_at=now)))  # fmt: skip
        return bool(res.rowcount == 1)

    def finish(
        self, source: str, batch: str, fix_id: str, attempt_no: int, status: Status,
        reason: str, now: datetime, next_at: datetime | None = None,
    ) -> None:  # fmt: skip
        if status not in ("succeeded", "failed", "skipped", "exhausted"):
            raise ReplayError("statut final inconnu")
        _code(reason)
        t, key = replay_attempt, self._key(source, batch, fix_id, attempt_no)
        self._exec(lambda c: c.execute(update(t).where(*key).values(
            status=status, reason_code=reason, finished_at=now, next_at=next_at)))  # fmt: skip

    def mark_exhausted(self, row_id: int, now: datetime) -> bool:
        """failed -> exhausted (conditional: exactly one caller wins, it sends the alert)."""
        t = replay_attempt
        res = self._exec(lambda c: c.execute(update(t).where(
            t.c.id == row_id, t.c.status == "failed").values(status="exhausted",
                                                             finished_at=now)))  # fmt: skip
        return bool(res.rowcount == 1)

    def expire_stale(self, now: datetime, timeout: timedelta) -> int:
        """planned/running older than `timeout` (crashed worker) -> failed/stale_timeout."""
        t = replay_attempt
        res = self._exec(lambda c: c.execute(update(t).where(
            t.c.status.in_(("planned", "running")), t.c.scheduled_at < now - timeout,
        ).values(status="failed", reason_code="stale_timeout", finished_at=now,
                 next_at=now)))  # fmt: skip
        return int(res.rowcount or 0)

    def window_count(self, now: datetime, window: timedelta) -> int:
        t = replay_attempt
        return int(self._exec(lambda c: c.execute(select(func.count()).where(
            t.c.status.in_(COUNTED), t.c.scheduled_at >= now - window)).scalar_one()))  # fmt: skip

    def reset(self, source: str, batch: str, by: str, reason: str, now: datetime) -> None:
        """Human reset of a batch budget (journaled row, nothing deleted)."""
        _check_source(source)
        who, why = (by or "").strip(), (reason or "").strip()
        if not who or len(who) > 128 or not who.isprintable():
            raise ReplayError("auteur obligatoire (1 à 128 caractères imprimables)")
        if not why or len(why) > _NOTE_MAX:
            raise ReplayError(f"raison obligatoire (1 à {_NOTE_MAX} caractères)")
        t = replay_attempt
        for _ in range(5):
            try:
                with self._engine.begin() as c:
                    n = c.execute(select(func.max(t.c.attempt_no)).where(
                        t.c.source_name == source, t.c.bronze_batch == batch,
                        t.c.fix_id == "reset")).scalar() or 0  # fmt: skip
                    c.execute(insert(t).values(
                        source_name=source, bronze_batch=batch, fix_id="reset",
                        attempt_no=n + 1, status="reset", reason_code="human_reset",
                        scheduled_at=now, finished_at=now, actor=who, note=why,
                    ))  # fmt: skip
                log.info("replay_reset", source=source, batch=batch, actor=who)
                return
            except IntegrityError:
                continue
            except SQLAlchemyError as exc:
                raise ReplayError(f"état de rejeu indisponible ({type(exc).__name__})") from None
        raise ReplayError("réinitialisation abandonnée après conflits")

    def attempts(self, source: str | None = None) -> list[dict[str, Any]]:
        t = replay_attempt
        stmt = select(t).order_by(t.c.id)
        if source is not None:
            stmt = stmt.where(t.c.source_name == source)
        rows = self._exec(lambda c: [dict(r._mapping) for r in c.execute(stmt)])
        for r in rows:
            r.pop("id")
        return rows  # type: ignore[no-any-return]


# --- execution -------------------------------------------------------------------------------
@dataclass(frozen=True)
class ReplayOutcome:
    status: Literal["succeeded", "blocked"]
    cause: str | None = None
    silver_skipped: bool = False
    gold_status: str | None = None


def replay_batch(
    spec: SourceSpec, bronze_batch: str, *, run_id: str, ctx: ReplayContext, learn: bool
) -> ReplayOutcome:
    """Re-run gate -> silver -> promote on the ORIGINAL Bronze batch (audited, trigger replay).

    Raises on technical errors (the caller counts them against the budget)."""
    from adp.mapping.llm import build_mapper
    from adp.orchestration.promotion_gate import promote_batch
    from adp.orchestration.schema_gate import read_bronze_table, run_schema_gate
    from adp.orchestration.silver_chain import run_silver_chain
    from adp.schema.events import EventBus
    from adp.schema.policy import load_policy
    from adp.schema.profile import profile_table

    audit, name = AuditStore(ctx.engine), spec.name
    batch_hash = bronze_batch.rsplit("/", 1)[-1].removesuffix(".parquet")
    if learn:  # approved synonyms -> learned file (idempotent), read by the silver chain
        update_learned_file(ctx.canonical_path, ctx.learned_path, ReviewQueue(ctx.engine).journal())
    with audited_step(audit, run_id=run_id, source=name, step="schema_check",
                      trigger=TRIGGER) as out:  # fmt: skip
        out.batch_id = batch_hash
        table = read_bronze_table(ctx.bronze_store, bronze_batch)
        gate = run_schema_gate(
            profile_table(table), source_name=name, primary_key=spec.primary_key,
            registry=SchemaRegistry(ctx.engine), policy=load_policy(ctx.policy_path),
            bus=EventBus(), store=ctx.bronze_store, run_id=run_id,
        )  # fmt: skip
        out.schema_version, out.schema_changes = gate.version, gate.n_changes
        out.impact = gate.overall_impact.value
        if gate.status == "blocked":
            out.status, out.reasons = "blocked", [gate.cause or "blocked"]
            return ReplayOutcome("blocked", gate.cause)
    with audited_step(audit, run_id=run_id, source=name, step="silver", trigger=TRIGGER) as out:
        dictionary = load_dictionary_with_learned(ctx.canonical_path, ctx.learned_path)
        from adp.config import get_settings

        factory = ctx.mapper_factory or (lambda d: build_mapper(get_settings(), d))
        try:
            res = run_silver_chain(
                table, spec=spec, schema_version=gate.version, dictionary=dictionary,
                mapper=factory(dictionary), queue=ReviewQueue(ctx.engine, dictionary),
                store=ctx.silver_store, run_id=run_id, semantic_dir=ctx.semantic_dir,
                bronze_path=bronze_batch,
            )  # fmt: skip
        except TransformError:
            out.status, out.reasons = "blocked", ["reject_threshold"]
            return ReplayOutcome("blocked", "reject_threshold")
        out.schema_version, out.rows_in = gate.version, res.rows_in
        if res.status == "blocked":
            out.status, out.reasons = "blocked", [res.cause or "blocked"]
            return ReplayOutcome("blocked", res.cause)
        mf = res.manifest
        out.rows_out = mf.row_count if mf else 0
    skipped = bool(mf and mf.skipped)
    if ctx.gold_store is None or ctx.quarantine_store is None or mf is None:
        return ReplayOutcome("succeeded", silver_skipped=skipped)
    with audited_step(audit, run_id=run_id, source=name, step="promote",
                      trigger=TRIGGER) as out:  # fmt: skip
        pr = promote_batch(
            source=name, silver_path=mf.parquet_path, run_id=run_id, dictionary=dictionary,
            silver_store=ctx.silver_store, gold_store=ctx.gold_store,
            quarantine_store=ctx.quarantine_store, report_dir=ctx.report_dir,
            engine=ctx.engine, schema_version=gate.version, quality_dir=ctx.quality_dir,
        )  # fmt: skip
        out.status = "success" if pr.status == "promoted" else "quarantined"
        out.rows_out, out.rows_quarantined, out.batch_id = pr.rows, pr.rows_quarantined, pr.batch_id
        out.quality_score = pr.score
    return ReplayOutcome("succeeded", silver_skipped=skipped, gold_status=pr.status)


@dataclass(frozen=True)
class ReplayReport:
    """What happened to one candidate in one cycle (codes only)."""

    source: str
    bronze_batch: str
    fix_id: str
    status: str
    reason: str
    attempt_no: int | None = None
    run_id: str | None = None


@dataclass
class _Cycle:
    ctx: ReplayContext
    store: ReplayStore
    load_policy: Callable[[], ReplayPolicy]
    now: Callable[[], datetime]
    rand: Callable[[], float]
    sender: AlertSender | None
    reports: list[ReplayReport] = field(default_factory=list)

    def report(self, c: ReplayCandidate, status: str, reason: str, **kw: Any) -> None:
        self.reports.append(ReplayReport(c.source, c.bronze_batch, c.fix_id, status, reason, **kw))
        log.info("replay_" + status, source=c.source, batch=c.bronze_batch, fix_id=c.fix_id,
                 reason=reason, **kw)  # fmt: skip


def _policy(load: Callable[[], ReplayPolicy]) -> ReplayPolicy:
    """Fail closed: an unreadable policy disables replay."""
    try:
        return load()
    except Exception as exc:
        log.warning("replay_policy_unreadable", error=type(exc).__name__)
        return ReplayPolicy()


def _exhausted_alert(cy: _Cycle, c: ReplayCandidate, attempts: int, run_id: str) -> None:
    try:
        policy = load_alert_policy(Path(os.environ.get("ADP_ALERTS_CONFIG", "config/alerts.yaml")))
        sender = cy.sender or sender_from_env(policy)
        if sender is None:
            return
        batch = c.bronze_batch.rsplit("/", 1)[-1].removesuffix(".parquet")[:12]
        alert = Alert(
            type="replay_exhausted", source=c.source, severity="critical",
            motif=f"replay_exhausted:{c.fix_id}:{batch}", title=f"Rejeu abandonné : {c.source}",
            summary=(f"Rejeu automatique abandonné après {attempts} tentative(s) (lot {batch}, "
                     f"correctif {c.reason}). Action humaine requise : nouveau correctif ou "
                     "adp replay reset."),
            run_id=run_id, links=(f"/audit/runs/{run_id}/steps",),
        )  # fmt: skip
        dispatch([alert], run_id, store=AlertStore(cy.ctx.engine), sender=sender, policy=policy,
                 now=cy.now())  # fmt: skip
    except Exception as exc:  # alerting never fails the cycle
        log.warning("replay_alert_failed", source=c.source, error=type(exc).__name__)


def _attempt(cy: _Cycle, c: ReplayCandidate, policy: ReplayPolicy) -> None:
    st = cy.store.state(c.source, c.bronze_batch, c.fix_id)
    if st.terminal:
        return cy.report(c, "skipped", st.terminal)
    if st.in_flight:
        return cy.report(c, "skipped", "in_flight")
    if st.counted >= policy.max_attempts:  # e.g. crashes used the budget, or budget lowered
        if st.last_failed_id is not None and cy.store.mark_exhausted(st.last_failed_id, cy.now()):
            _exhausted_alert(cy, c, st.counted, f"replay-{_hash(c.source, c.bronze_batch)}")
            return cy.report(c, "exhausted", "budget")
        return cy.report(c, "skipped", "exhausted")
    if st.next_at is not None and cy.now() < st.next_at:
        return cy.report(c, "skipped", "backoff")
    window = timedelta(minutes=policy.window_minutes)
    if cy.store.window_count(cy.now(), window) >= policy.max_per_window:
        return cy.report(c, "skipped", "window_budget")
    n = st.next_no
    run_id = f"replay-{_hash(c.source, c.bronze_batch, c.fix_id)}-{n}"
    if policy.dry_run:
        return cy.report(c, "planned", c.reason, attempt_no=n, run_id=run_id)
    if not cy.store.reserve(c.source, c.bronze_batch, c.fix_id, n, cy.now(), run_id):
        return cy.report(c, "skipped", "concurrent")
    why = kill_switch(c.source, _policy(cy.load_policy))  # re-checked right before execution
    if why is not None:
        cy.store.finish(c.source, c.bronze_batch, c.fix_id, n, "skipped", why, cy.now())
        return cy.report(c, "skipped", why, attempt_no=n)
    if not cy.store.start(c.source, c.bronze_batch, c.fix_id, n, cy.now()):
        return cy.report(c, "skipped", "concurrent", attempt_no=n)
    spec = cy.ctx.sources[c.source]
    try:
        res = replay_batch(spec, c.bronze_batch, run_id=run_id, ctx=cy.ctx,
                           learn=c.reason == "mapping_approved")  # fmt: skip
        error = None
    except Exception as exc:
        res, error = None, f"error:{type(exc.__cause__ or exc).__name__}"[:64]
    now = cy.now()
    if res is not None and res.status == "succeeded":
        cy.store.finish(c.source, c.bronze_batch, c.fix_id, n, "succeeded", c.reason, now)
        cy.report(c, "succeeded", c.reason, attempt_no=n, run_id=run_id)
    elif res is not None:  # blocked again: terminal for this fix, never looped
        reason = f"blocked:{res.cause or 'unknown'}"[:64]
        cy.store.finish(c.source, c.bronze_batch, c.fix_id, n, "failed", reason, now)
        cy.report(c, "failed", reason, attempt_no=n, run_id=run_id)
    elif st.counted + 1 >= policy.max_attempts:
        cy.store.finish(c.source, c.bronze_batch, c.fix_id, n, "exhausted", str(error), now)
        cy.report(c, "exhausted", str(error), attempt_no=n, run_id=run_id)
        _exhausted_alert(cy, c, st.counted + 1, run_id)
    else:
        delay = backoff_delay(st.counted + 1, policy.backoff, cy.rand())
        cy.store.finish(c.source, c.bronze_batch, c.fix_id, n, "failed", str(error), now,
                        next_at=now + timedelta(seconds=delay))  # fmt: skip
        cy.report(c, "failed", str(error), attempt_no=n, run_id=run_id)
    notify_run(AuditStore(cy.ctx.engine), run_id, c.source, step="promote", sender=cy.sender)


def run_replays(
    ctx: ReplayContext,
    *,
    load_policy: Callable[[], ReplayPolicy] = _policy_from_env,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    rand: Callable[[], float] | None = None,
    sender: AlertSender | None = None,
    dry_run: bool = False,
) -> list[ReplayReport]:
    """One replay cycle (sensor or CLI). Returns what happened per candidate."""
    policy = _policy(load_policy)
    if kill_switch("", policy) in ("env_disabled", "global_disabled"):
        return []  # switched off: no read, no write, no attempt
    if dry_run and not policy.dry_run:
        policy = policy.model_copy(update={"dry_run": True})
    store = ReplayStore(ctx.engine)
    if not policy.dry_run:
        store.expire_stale(now(), timedelta(minutes=policy.running_timeout_minutes))
    fps = config_fingerprints(ctx)
    blocked = []
    for b in find_blocked(ctx):
        fp = fps[CONFIG_OF_CAUSE[b.cause]]
        base = store.ensure_baseline(b.source, b.bronze_batch, b.block_run_id, fp, now(),
                                     write=not policy.dry_run)  # fmt: skip
        blocked.append(replace(b, config_baseline=base))
    candidates = detect_fixes(blocked, ReviewQueue(ctx.engine).journal(), fps)
    cy = _Cycle(ctx, store, load_policy, now, rand or random.SystemRandom().random, sender)
    executed = 0
    for c in candidates:
        if executed >= policy.max_per_cycle:
            break
        p = _policy(load_policy)  # re-read for every candidate
        if dry_run:
            p = p.model_copy(update={"dry_run": True})
        why = kill_switch(c.source, p)
        if why is not None:
            cy.report(c, "skipped", why)
            continue
        before = len(cy.reports)
        _attempt(cy, c, p)
        executed += any(r.status in ("succeeded", "failed", "exhausted")
                        for r in cy.reports[before:])  # fmt: skip
    return cy.reports


# --- Dagster sensor --------------------------------------------------------------------------
def make_replay_sensor(registry: Mapping[str, SourceSpec]) -> Any:
    """`replay_sensor`: STOPPED by default, long interval; never raises into the daemon."""
    from dagster import DefaultSensorStatus, SensorEvaluationContext, SkipReason, sensor

    @sensor(name="replay_sensor", minimum_interval_seconds=300,
            default_status=DefaultSensorStatus.STOPPED)  # fmt: skip
    def _sensor(context: SensorEvaluationContext) -> SkipReason:
        try:
            reports = run_replays(ReplayContext.from_env(registry))
        except Exception as exc:  # the daemon must survive anything
            log.warning("replay_sensor_failed", error=type(exc).__name__)
            return SkipReason(f"replay cycle failed ({type(exc).__name__})")
        done = [r for r in reports if r.status in ("succeeded", "failed", "exhausted")]
        return SkipReason(f"replay cycle: {len(done)} attempt(s), {len(reports)} candidate(s)")

    return _sensor
