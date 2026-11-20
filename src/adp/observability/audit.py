"""Run and step audit journal (P6-01): the persistent run log (closes H-34).

Tables `run_audit` (one row per (run_id, source)) and `step_audit` (one row per step of a run),
DDL owned by Alembic revision 0005. Decisions:
- Append-friendly and idempotent: a step is written once per (run_id, source, step). Replaying
  the same result (timestamps ignored) is a no-op; a different result raises AuditError, like
  `quality_score` (history is never rewritten).
- The run row is DERIVED from its steps inside the same transaction (no separate "finish"
  call, no state to forget): status = failed > blocked > quarantined > success, finished_at =
  last step end, counts/ids = those of the latest step that carries them.
- Only counts, ids, enums, reason codes and exception TYPE names are stored: never a data value,
  a message or a secret.
- `audited_step` records a step around a block of code; an audit failure NEVER fails the data
  run (structured warning only).
"""

import re
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from adp.errors import AuditError
from adp.logging import get_logger
from adp.schema.registry import _check_source, metadata

log = get_logger(__name__)

STEPS = ("bronze", "schema_check", "silver", "promote")  # chain order
STATUSES = ("success", "failed", "blocked", "quarantined")
RUN_STATUSES = (*STATUSES, "running")
_SEVERITY = ("failed", "blocked", "quarantined")  # worst first
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_CODE = re.compile(r"^[A-Za-z0-9_:.-]{1,64}$")
_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_MAX_REASONS = 20
_MAX_RETRIES = 5


def _in(col: str, values: Sequence[str]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


run_audit = Table(
    "run_audit",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String(64), nullable=False),
    Column("source_name", String(255), nullable=False),
    Column("trigger", String(16), nullable=False),
    Column("status", String(16), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    Column("schema_version", Integer, nullable=True),
    Column("rows_in", Integer, nullable=True),
    Column("rows_out", Integer, nullable=True),
    Column("rows_quarantined", Integer, nullable=True),
    Column("quality_score", Float, nullable=True),
    Column("batch_id", String(128), nullable=True),
    Column("error_type", String(64), nullable=True),
    UniqueConstraint("run_id", "source_name", name="uq_run_audit_run"),
    CheckConstraint(_in("status", RUN_STATUSES), name="ck_run_audit_status"),
    Index("ix_run_audit_source_started", "source_name", "started_at"),
)

step_audit = Table(
    "step_audit",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String(64), nullable=False),
    Column("source_name", String(255), nullable=False),
    Column("step", String(16), nullable=False),
    Column("status", String(16), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True), nullable=False),
    Column("duration_ms", Integer, nullable=False),
    Column("rows_in", Integer, nullable=True),
    Column("rows_out", Integer, nullable=True),
    Column("rows_quarantined", Integer, nullable=True),
    Column("quality_score", Float, nullable=True),
    Column("schema_version", Integer, nullable=True),
    Column("schema_changes", Integer, nullable=True),
    Column("impact", String(16), nullable=True),
    Column("batch_id", String(128), nullable=True),
    Column("reasons", Text, nullable=False, server_default=""),  # codes joined by a space
    Column("error_type", String(64), nullable=True),
    UniqueConstraint("run_id", "source_name", "step", name="uq_step_audit_step"),
    ForeignKeyConstraint(
        ["run_id", "source_name"],
        ["run_audit.run_id", "run_audit.source_name"],
        name="fk_step_audit_run",
    ),  # fmt: skip
    CheckConstraint(_in("status", STATUSES), name="ck_step_audit_status"),
    CheckConstraint(_in("step", STEPS), name="ck_step_audit_step"),
    Index("ix_step_audit_step", "step", "source_name"),
)

# Fields compared on replay (timestamps and duration are not part of the "result").
_RESULT = (
    "status", "rows_in", "rows_out", "rows_quarantined", "quality_score", "schema_version",
    "schema_changes", "impact", "batch_id", "reasons", "error_type",
)  # fmt: skip
_RUN_FIELDS = (
    "schema_version",
    "rows_in",
    "rows_out",
    "rows_quarantined",
    "quality_score",
    "batch_id",
)


@dataclass
class StepOutcome:
    """Mutable result of a step, filled by the audited block (counts and codes only)."""

    status: str = "success"
    rows_in: int | None = None
    rows_out: int | None = None
    rows_quarantined: int | None = None
    quality_score: float | None = None
    schema_version: int | None = None
    schema_changes: int | None = None
    impact: str | None = None
    batch_id: str | None = None
    reasons: list[str] = field(default_factory=list)
    error_type: str | None = None


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # SQLite drops the time zone


def _count(name: str, v: int | None) -> int | None:
    if v is not None and (type(v) is not int or v < 0):
        raise AuditError(f"{name} doit être un entier positif ou nul")
    return v


def _validate(run_id: str, source: str, step: str, o: StepOutcome) -> str:
    """Check every field; returns the reasons string. Raises AuditError."""
    try:
        _check_source(source)
    except Exception as e:
        raise AuditError("nom de source invalide") from e
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise AuditError("run_id invalide (1 à 64 caractères [A-Za-z0-9._:-])")
    if step not in STEPS:
        raise AuditError(f"étape inconnue (attendu : {', '.join(STEPS)})")
    if o.status not in STATUSES:
        raise AuditError(f"statut inconnu (attendu : {', '.join(STATUSES)})")
    for n in ("rows_in", "rows_out", "rows_quarantined", "schema_version", "schema_changes"):
        _count(n, getattr(o, n))
    if o.quality_score is not None and not 0 <= o.quality_score <= 100:
        raise AuditError("quality_score hors de [0, 100]")
    if o.error_type is not None and not _TYPE.fullmatch(o.error_type):
        raise AuditError("error_type doit être un nom de type, jamais un message")
    if o.batch_id is not None and not _CODE.fullmatch(o.batch_id):
        raise AuditError("batch_id invalide")
    if o.impact is not None and o.impact not in ("compatible", "review", "blocking"):
        raise AuditError("impact inconnu")
    if len(o.reasons) > _MAX_REASONS or not all(_CODE.fullmatch(r) for r in o.reasons):
        raise AuditError("reasons : codes [A-Za-z0-9_:.-]{1,64} uniquement")
    return " ".join(o.reasons)


def _derive(conn: Connection, run_id: str, source: str) -> dict[str, Any]:
    """Run-level fields computed from all its steps."""
    t = step_audit
    rows = sorted(
        conn.execute(select(t).where(t.c.run_id == run_id, t.c.source_name == source)),
        key=lambda r: STEPS.index(r.step),
    )
    status = next((s for s in _SEVERITY if any(r.status == s for r in rows)), "success")
    out: dict[str, Any] = {
        "status": status if rows else "running",
        "finished_at": max((_utc(r.finished_at) for r in rows), default=None),
        "error_type": next((r.error_type for r in rows if r.error_type), None),
    }
    for f in _RUN_FIELDS:  # latest step carrying the value, except rows_in (the first)
        vals = [getattr(r, f) for r in rows if getattr(r, f) is not None]
        out[f] = (vals[0] if f == "rows_in" else vals[-1]) if vals else None
    return out


class AuditStore:
    """Persistence of the audit journal. Raises AuditError; never stores data values."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @property
    def engine(self) -> Engine:
        return self._engine

    def start_run(
        self, run_id: str, source: str, *, trigger: str = "dagster",
        started_at: datetime | None = None,
    ) -> None:  # fmt: skip
        """Create the run row (idempotent). A different trigger for the same run is an error."""
        _validate(run_id, source, "bronze", StepOutcome())
        if not _CODE.fullmatch(trigger) or len(trigger) > 16:
            raise AuditError("trigger invalide")
        try:
            with self._engine.begin() as conn:
                self._ensure_run(conn, run_id, source, trigger, started_at or datetime.now(UTC))
        except SQLAlchemyError as e:
            raise AuditError(f"écriture de l'audit impossible ({type(e).__name__})") from e

    @staticmethod
    def _ensure_run(
        conn: Connection, run_id: str, source: str, trigger: str | None, started_at: datetime
    ) -> None:
        t = run_audit
        row = conn.execute(
            select(t.c.trigger).where(t.c.run_id == run_id, t.c.source_name == source)
        ).first()
        if row is None:
            conn.execute(insert(t).values(
                run_id=run_id, source_name=source, trigger=trigger or "dagster", status="running",
                started_at=started_at,
            ))  # fmt: skip
        elif trigger is not None and row.trigger != trigger:
            raise AuditError(f"{source}/{run_id} : run déjà enregistré avec un autre déclencheur")

    def record_step(
        self, run_id: str, source: str, step: str, outcome: StepOutcome, *,
        started_at: datetime, finished_at: datetime, trigger: str | None = None,
    ) -> bool:  # fmt: skip
        """Write one step and refresh the run row, in ONE transaction. True if inserted,
        False on an identical replay; a different result for the same key raises AuditError."""
        reasons = _validate(run_id, source, step, outcome)
        started_at, finished_at = _utc(started_at), _utc(finished_at)
        new = {f: getattr(outcome, f) for f in _RESULT if f != "reasons"} | {"reasons": reasons}
        t = step_audit
        for _ in range(_MAX_RETRIES):
            try:
                with self._engine.begin() as conn:
                    self._ensure_run(conn, run_id, source, trigger, started_at)
                    old = conn.execute(
                        select(t).where(t.c.run_id == run_id, t.c.source_name == source,
                                        t.c.step == step)
                    ).first()  # fmt: skip
                    if old is not None:
                        if any(getattr(old, f) != new[f] for f in _RESULT):
                            raise AuditError(
                                f"{source}/{run_id}/{step} : étape déjà enregistrée et différente"
                            )
                        return False
                    duration = max(0, round((finished_at - started_at).total_seconds() * 1000))
                    conn.execute(insert(t).values(
                        run_id=run_id, source_name=source, step=step, started_at=started_at,
                        finished_at=finished_at, duration_ms=duration, **new,
                    ))  # fmt: skip
                    r = run_audit
                    cur = conn.execute(select(r.c.started_at).where(
                        r.c.run_id == run_id, r.c.source_name == source)).scalar_one()  # fmt: skip
                    conn.execute(update(r).where(r.c.run_id == run_id,
                                                 r.c.source_name == source).values(
                        _derive(conn, run_id, source) | {"started_at": min(_utc(cur), started_at)}
                    ))  # fmt: skip
                    return True
            except IntegrityError:
                continue  # concurrent write of the same key: replay (compares, no-op)
            except SQLAlchemyError as e:
                raise AuditError(f"écriture de l'audit impossible ({type(e).__name__})") from e
        raise AuditError(f"{source}/{run_id}/{step} : écriture abandonnée après conflits")

    def runs(
        self, source: str | None = None, limit: int = 200, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Runs, newest first (read-only)."""
        t = run_audit
        stmt = select(t).order_by(t.c.started_at.desc(), t.c.id.desc())
        if source is not None:
            stmt = stmt.where(t.c.source_name == _check_source(source))
        return self._rows(stmt.limit(limit).offset(offset))

    def steps(self, run_id: str, source: str | None = None) -> list[dict[str, Any]]:
        """Steps of a run in chain order (read-only)."""
        t = step_audit
        stmt = select(t).where(t.c.run_id == run_id)
        if source is not None:
            stmt = stmt.where(t.c.source_name == source)
        return sorted(self._rows(stmt), key=lambda r: STEPS.index(r["step"]))

    def _rows(self, stmt: Any) -> list[dict[str, Any]]:
        try:
            with self._engine.connect() as conn:
                out = [dict(r._mapping) for r in conn.execute(stmt)]
        except SQLAlchemyError as e:
            raise AuditError(f"lecture de l'audit impossible ({type(e).__name__})") from e
        for r in out:
            r.pop("id", None)
            if "reasons" in r:
                r["reasons"] = r["reasons"].split()
        return out


@contextmanager
def audited_step(
    store: AuditStore | None, *, run_id: str, source: str, step: str, trigger: str | None = None
) -> Iterator[StepOutcome]:
    """Record a step around a block. The block fills the yielded outcome; an exception marks it
    `failed` (type name only, unless the block already set a status) and is re-raised.
    Audit errors are logged as a warning and never raised."""
    out = StepOutcome()
    started, t0 = datetime.now(UTC), time.perf_counter()
    try:
        yield out
    except BaseException as exc:
        if out.status == "success":
            out.status, out.error_type = "failed", type(exc.__cause__ or exc).__name__
        _safe_record(store, run_id, source, step, out, started, t0, trigger)
        raise
    _safe_record(store, run_id, source, step, out, started, t0, trigger)


def _safe_record(
    store: AuditStore | None, run_id: str, source: str, step: str, out: StepOutcome,
    started: datetime, t0: float, trigger: str | None,
) -> None:  # fmt: skip
    if store is None:
        return
    finished = datetime.fromtimestamp(started.timestamp() + (time.perf_counter() - t0), UTC)
    try:
        store.record_step(run_id, source, step, out, started_at=started, finished_at=finished,
                          trigger=trigger)  # fmt: skip
    except Exception as exc:  # audit must never fail a data run
        log.warning("audit_failed", source=source, run_id=run_id, step=step,
                    error=type(exc).__name__)  # fmt: skip
