"""Mapping decision policy (H-02) and human review queue (human-in-the-loop).

Pure policy (`decide`, `decide_batch`, `combine`), no I/O:
- Thresholds: `DecisionConfig` is THE reference of the mapping side. Its defaults are the H-02
  constants of `adp.mapping.llm` (AUTO_THRESHOLD 0.90, REVIEW_THRESHOLD 0.60) that
  `confidence_band`/`trace_record` and the local fallback cap already use; `RulesConfig.min_score`
  and `DiffConfig` (schema side) carry the same values, a test pins the consistency.
  score >= auto -> auto_apply; score >= review -> review; else reject.
- Guards only ever DOWNGRADE (auto -> review), never upgrade a reject:
  * target None (LLM abstention) -> reject: there is nothing to approve, and queuing empty
    proposals would flood reviewers; the column simply stays unmapped (visible downstream).
  * tie (two candidates with the same score) -> review: the choice is arbitrary.
  * type_compatible False -> review even at 1.0: applying a name match onto a field of an
    incompatible type corrupts Silver silently (casts fail or coerce); a human must confirm.
  * origin `local` (fallback without LLM, already capped at 0.89) -> never auto, whatever its
    score: it is a guess on columns the rules could not resolve.
  * method `fuzzy` -> never auto unless corroborated by an independent LLM giving the same
    target: lexical similarity has no semantics (taux_chomage_h vs taux_chomage_f).
  * rules and LLM disagree on the target -> review (the higher score is proposed).
  * batch: two distinct source columns proposing the same target -> both go to review.
- `combine(rules, llm)`: agreement keeps the max score and marks it corroborated (only when
  the LLM is a real one: the local fallback reuses the rules, it is not independent evidence).

Persistence (`ReviewQueue`, portable SQLAlchemy Core on the registry `metadata`, DDL owned
by Alembic revision 0003):
- `review_queue`: one row per (source_name, source_col, target, prompt_version) -> re-submitting
  the same proposal is a no-op (idempotence); a human decision is final for that key (a rejected
  proposal is not re-queued, and an auto_apply of a human-rejected key is vetoed). A new
  proposal for a column supersedes its older pending rows.
- `mapping_decision_log`: append-only journal of EVERY decision (auto_apply, approve, reject,
  revert, supersede), actor 'system' or a human id; an auto_apply that merely re-confirms
  the column's latest decision (same system auto_apply, or a human approve of the same target,
  e.g. resolved next run by the learned synonym) is not re-journaled. The API never updates or
  deletes it (enforcing it at DB level = GRANT INSERT/SELECT only, deployment concern).
- Decisions use a conditional `UPDATE ... WHERE status = 'pending'`: of two concurrent
  approvals exactly one wins, the other gets a MappingError (no double decision).
- `revert(log_id)`: only the latest decision of a column; journals 'revert' and puts the item
  back to pending (an auto_apply gets a pending row). Nothing is ever deleted.
Nothing is applied to Silver here (P3-05/06): auto-applied mappings are only returned.
No data value is stored: column names, field names, scores and short cleaned rationales only.
"""

from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import (
    Column,
    DateTime,
    Engine,
    Float,
    Integer,
    Row,
    String,
    Table,
    UniqueConstraint,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from adp.errors import MappingError
from adp.logging import get_logger
from adp.mapping.dictionary import SemanticDictionary
from adp.mapping.llm import AUTO_THRESHOLD, RATIONALE_MAX, REVIEW_THRESHOLD, MappingProposal
from adp.mapping.rules import MappingCandidate
from adp.schema.registry import _check_source, metadata

log = get_logger(__name__)

Action = Literal["auto_apply", "review", "reject"]
Origin = Literal["rules", "llm", "local"]
Status = Literal["pending", "approved", "rejected", "superseded"]
LogAction = Literal["auto_apply", "approve", "reject", "revert", "supersede"]
STATUSES: frozenset[str] = frozenset({"pending", "approved", "rejected", "superseded"})
SYSTEM = "system"
_DECISIONS = ("auto_apply", "approve", "reject", "revert")  # supersede is housekeeping
_MAX_RETRIES = 5
_NOTE_MAX = 500

review_queue = Table(
    "review_queue",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source_name", String(255), nullable=False),
    Column("source_col", String(255), nullable=False),
    Column("target", String(255), nullable=False),
    Column("final_target", String(255), nullable=True),
    Column("score", Float, nullable=False),
    Column("method", String(32), nullable=False),
    Column("origin", String(16), nullable=False),
    Column("model", String(128), nullable=False),
    Column("prompt_version", String(64), nullable=False),
    Column("rationale", String(RATIONALE_MAX), nullable=False),
    Column("reason", String(128), nullable=False),
    Column("status", String(16), nullable=False, index=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("decided_at", DateTime(timezone=True), nullable=True),
    Column("decided_by", String(128), nullable=True),
    Column("decision_note", String(_NOTE_MAX), nullable=True),
    UniqueConstraint(
        "source_name", "source_col", "target", "prompt_version", name="uq_review_queue_proposal"
    ),
)

mapping_decision_log = Table(
    "mapping_decision_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("queue_id", Integer, nullable=True),
    Column("source_name", String(255), nullable=False),
    Column("source_col", String(255), nullable=False),
    Column("target", String(255), nullable=True),
    Column("action", String(16), nullable=False),
    Column("actor", String(128), nullable=False),
    Column("score", Float, nullable=False),
    Column("method", String(32), nullable=False),
    Column("origin", String(16), nullable=False),
    Column("model", String(128), nullable=False),
    Column("prompt_version", String(64), nullable=False),
    Column("reason", String(128), nullable=False),
    Column("decided_at", DateTime(timezone=True), nullable=False),
    Column("note", String(_NOTE_MAX), nullable=True),
)


# --- pure policy ---------------------------------------------------------------------------
class DecisionConfig(BaseModel):
    """H-02 thresholds (0 < review <= auto <= 1) and review-only origins/methods."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    review_threshold: float = Field(default=REVIEW_THRESHOLD, gt=0, le=1)
    auto_threshold: float = Field(default=AUTO_THRESHOLD, gt=0, le=1)
    review_only_origins: frozenset[str] = frozenset({"local"})
    review_only_methods: frozenset[str] = frozenset({"fuzzy"})  # unless corroborated

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.review_threshold > self.auto_threshold:
            raise ValueError("review_threshold must be <= auto_threshold")
        return self


class Proposal(BaseModel):
    """Common form of a rules candidate or an LLM/local proposal (never applied here)."""

    model_config = ConfigDict(frozen=True)

    source_col: str = Field(min_length=1, max_length=255)
    target: str | None = Field(default=None, max_length=255)
    score: float = Field(ge=0, le=1)
    method: str = Field(min_length=1, max_length=32)
    origin: Origin
    tie: bool = False
    type_compatible: bool | None = None
    corroborated: bool = False
    disputed: bool = False
    model: str = Field(default="", max_length=128)
    prompt_version: str = Field(default="", max_length=64)
    rationale: str = Field(default="", max_length=RATIONALE_MAX)


class Decision(BaseModel):
    """Policy outcome for one proposal; `reasons` are short codes, no data value."""

    model_config = ConfigDict(frozen=True)

    proposal: Proposal
    action: Action
    reasons: tuple[str, ...]


ProposalLike = Proposal | MappingCandidate | MappingProposal


def to_proposal(item: ProposalLike) -> Proposal:
    """Normalise a rules candidate / LLM proposal."""
    if isinstance(item, Proposal):
        return item
    if isinstance(item, MappingCandidate):
        return Proposal(
            source_col=item.source_col, target=item.target, score=item.score,
            method=item.method, origin="rules", tie=item.tie,
            type_compatible=item.type_compatible, rationale=item.detail[:RATIONALE_MAX],
        )  # fmt: skip
    return Proposal(
        source_col=item.source_col, target=item.target, score=item.confidence,
        method=item.source, origin=item.source, model=item.model,
        prompt_version=item.prompt_version, rationale=item.rationale[:RATIONALE_MAX],
    )  # fmt: skip


def combine(rule: MappingCandidate | None, proposal: MappingProposal | None) -> Proposal | None:
    """Merge the rules candidate and the LLM proposal of one column (None if both absent).

    Abstention of the LLM leaves the rules candidate alone; agreement with a real LLM keeps the
    max score and sets `corroborated`; disagreement proposes the higher score, `disputed`.
    """
    r = to_proposal(rule) if rule is not None else None
    p = to_proposal(proposal) if proposal is not None else None
    if r is None or p is None or p.target is None:
        return r or p
    if p.target == r.target:
        if p.origin != "llm":
            return r  # the local fallback reuses the rules: not independent evidence
        why = f"rules:{r.method} + llm:{p.model}/{p.prompt_version}"
        return r.model_copy(
            update={"score": max(r.score, p.score), "corroborated": True,
                    "rationale": why[:RATIONALE_MAX]}
        )  # fmt: skip
    best = r if r.score >= p.score else p
    why = f"rules:{r.target} vs {p.origin}:{p.target}"
    return best.model_copy(update={"disputed": True, "rationale": why[:RATIONALE_MAX]})


def decide(item: ProposalLike, config: DecisionConfig = DecisionConfig()) -> Decision:  # noqa: B008
    """Apply H-02 thresholds then the downgrade-only guards (see module docstring)."""
    p = to_proposal(item)
    if p.target is None:
        return Decision(proposal=p, action="reject", reasons=("abstention",))
    if p.score < config.review_threshold:
        return Decision(proposal=p, action="reject", reasons=("below_review",))
    guards = [
        code
        for code, hit in (
            ("tie", p.tie),
            ("type_incompatible", p.type_compatible is False),
            ("rules_llm_disagree", p.disputed),
            (
                "review_only",
                p.origin in config.review_only_origins
                or (p.method in config.review_only_methods and not p.corroborated),
            ),
        )  # fmt: skip
        if hit
    ]
    if p.score >= config.auto_threshold and not guards:
        return Decision(proposal=p, action="auto_apply", reasons=("above_auto",))
    return Decision(proposal=p, action="review", reasons=tuple(guards) or ("review_band",))


def decide_batch(
    items: Iterable[ProposalLike],
    config: DecisionConfig = DecisionConfig(),  # noqa: B008
) -> list[Decision]:
    """`decide` each item (one per source column), then send target conflicts to review."""
    out = [decide(i, config) for i in items]
    dup = [c for c, n in Counter(d.proposal.source_col for d in out).items() if n > 1]
    if dup:
        raise MappingError(f"colonne source en double dans le lot ({len(dup)} cas)")
    targets = Counter(d.proposal.target for d in out if d.action != "reject")
    return [
        d.model_copy(update={"action": "review", "reasons": (*d.reasons, "target_conflict")})
        if d.action != "reject" and targets[d.proposal.target] > 1
        else d
        for d in out
    ]


# --- persistence ---------------------------------------------------------------------------
class ReviewItem(BaseModel):
    """One row of the review queue."""

    model_config = ConfigDict(frozen=True)

    id: int
    source_name: str
    source_col: str
    target: str
    final_target: str | None
    score: float
    method: str
    origin: str
    model: str
    prompt_version: str
    rationale: str
    reason: str
    status: Status
    created_at: datetime
    decided_at: datetime | None
    decided_by: str | None
    decision_note: str | None


class LogEntry(BaseModel):
    """One journal entry (immutable)."""

    model_config = ConfigDict(frozen=True)

    id: int
    queue_id: int | None
    source_name: str
    source_col: str
    target: str | None
    action: LogAction
    actor: str
    score: float
    method: str
    origin: str
    model: str
    prompt_version: str
    reason: str
    decided_at: datetime
    note: str | None


class SubmitResult(BaseModel):
    """Outcome of a submit. `auto_applied` is to be applied by P3-05/06, not here."""

    model_config = ConfigDict(frozen=True)

    decisions: tuple[Decision, ...]
    auto_applied: list[Decision]
    queued: list[int]  # ids of the pending rows (new or already there)
    rejected: list[Decision]
    vetoed: list[Decision]  # auto_apply blocked by an earlier human rejection of that key


def _utc(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(UTC)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # SQLite drops the time zone


def _actor(actor: str) -> str:
    a = (actor or "").strip()
    if not a or len(a) > 128 or not a.isprintable():
        raise MappingError("auteur obligatoire (1 à 128 caractères imprimables)")
    return a


def _note(note: str | None) -> str | None:
    if note is not None and len(note) > _NOTE_MAX:
        raise MappingError(f"note trop longue (max {_NOTE_MAX} caractères)")
    return note or None


def _item(row: Row[Any]) -> ReviewItem:
    d = row._asdict()
    d["created_at"] = _utc(d["created_at"])
    d["decided_at"] = _utc(d["decided_at"]) if d["decided_at"] else None
    return ReviewItem.model_validate(d)


def _entry(row: Row[Any]) -> LogEntry:
    d = row._asdict()
    d["decided_at"] = _utc(d["decided_at"])
    return LogEntry.model_validate(d)


def _log_values(src: Any, **kw: Any) -> dict[str, Any]:
    """Journal row copied from a proposal-like object (Proposal, queue row or log row)."""
    base = {
        k: getattr(src, k)
        for k in ("source_col", "target", "score", "method", "origin", "model", "prompt_version")
    }
    return base | kw


class ReviewQueue:
    """Submit proposals, review them, read the append-only journal. Raises MappingError."""

    def __init__(self, engine: Engine, dictionary: SemanticDictionary | None = None) -> None:
        self._engine = engine
        self._fields = {f.name for f in dictionary.fields} if dictionary else None

    def submit(
        self,
        items: Iterable[ProposalLike],
        source_name: str,
        config: DecisionConfig = DecisionConfig(),  # noqa: B008
        now: datetime | None = None,
    ) -> SubmitResult:
        """Decide a batch (one proposal per column) and persist it, idempotently."""
        _check_source(source_name)
        decisions = decide_batch(items, config)
        ts = _utc(now)
        for _ in range(_MAX_RETRIES):
            try:
                with self._engine.begin() as conn:
                    queued, vetoed = self._persist(conn, decisions, source_name, ts)
                break
            except IntegrityError:
                continue  # a concurrent submit inserted the same key: replay (idempotent)
            except SQLAlchemyError as e:
                raise MappingError(
                    f"écriture de la file de revue impossible ({type(e).__name__})"
                ) from e
        else:
            raise MappingError(
                f"{source_name} : soumission abandonnée après {_MAX_RETRIES} conflits"
            )
        auto = [d for d in decisions if d.action == "auto_apply" and d not in vetoed]
        rejected = [d for d in decisions if d.action == "reject"]
        log.info(
            "mapping_submitted", source_name=source_name, auto=len(auto), queued=len(queued),
            rejected=len(rejected), vetoed=len(vetoed),
        )  # fmt: skip
        return SubmitResult(
            decisions=tuple(decisions), auto_applied=auto, queued=queued,
            rejected=rejected, vetoed=vetoed,
        )  # fmt: skip

    def _persist(
        self, conn: Any, decisions: Sequence[Decision], source_name: str, ts: datetime
    ) -> tuple[list[int], list[Decision]]:
        q, lg = review_queue, mapping_decision_log
        queued: list[int] = []
        vetoed: list[Decision] = []
        for d in decisions:
            p = d.proposal
            reason = ",".join(d.reasons)[:128]
            col = (q.c.source_name == source_name, q.c.source_col == p.source_col)
            key = (*col, q.c.target == p.target, q.c.prompt_version == p.prompt_version)
            existing = conn.execute(select(q).where(*key)).first() if p.target else None
            if d.action != "reject":
                for old in conn.execute(select(q).where(*col, q.c.status == "pending")).all():
                    if existing is not None and old.id == existing.id:
                        continue
                    conn.execute(
                        update(q).where(q.c.id == old.id, q.c.status == "pending")
                        .values(status="superseded", decided_at=ts, decided_by=SYSTEM)
                    )  # fmt: skip
                    conn.execute(insert(lg).values(_log_values(
                        old, queue_id=old.id, source_name=source_name, action="supersede",
                        actor=SYSTEM, reason="superseded", decided_at=ts, note=None,
                    )))  # fmt: skip
            if d.action == "review":
                if existing is None:
                    res = conn.execute(insert(q).values(
                        source_name=source_name, source_col=p.source_col, target=p.target,
                        score=p.score, method=p.method, origin=p.origin, model=p.model,
                        prompt_version=p.prompt_version, rationale=p.rationale, reason=reason,
                        status="pending", created_at=ts,
                    ))  # fmt: skip
                    queued.append(int(res.inserted_primary_key[0]))
                elif existing.status in ("pending", "superseded"):
                    conn.execute(
                        update(q).where(q.c.id == existing.id)
                        .values(status="pending", decided_at=None, decided_by=None)
                    )  # fmt: skip
                    queued.append(existing.id)
                continue  # approved/rejected: the human decision for this key stands
            if d.action == "auto_apply" and existing is not None and existing.status == "rejected":
                vetoed.append(d)
                continue
            last = conn.execute(
                select(lg).where(
                    lg.c.source_name == source_name, lg.c.source_col == p.source_col,
                    lg.c.action.in_(_DECISIONS),
                ).order_by(lg.c.id.desc()).limit(1)
            ).first()  # fmt: skip
            same = last is not None and (
                last.action,
                last.target,
                last.prompt_version,
                last.actor,
            ) == (d.action, p.target, p.prompt_version, SYSTEM)
            # an auto_apply re-confirming a standing HUMAN approval of the same target is not
            # journaled: it would become the column's latest decision and un-learn it (learn.py)
            confirms = (
                last is not None
                and d.action == "auto_apply"
                and (last.action, last.target) == ("approve", p.target)
            )
            if not same and not confirms:
                conn.execute(insert(lg).values(_log_values(
                    p, queue_id=None, source_name=source_name, action=d.action, actor=SYSTEM,
                    reason=reason, decided_at=ts, note=None,
                )))  # fmt: skip
        return queued, vetoed

    def get(self, item_id: int) -> ReviewItem | None:
        """One queue row, or None."""
        rows = self._read(select(review_queue).where(review_queue.c.id == item_id))
        return _item(rows[0]) if rows else None

    def journal(
        self, source_name: str | None = None, source_col: str | None = None
    ) -> list[LogEntry]:
        """Journal entries in insertion order (read-only)."""
        lg = mapping_decision_log
        stmt = select(lg).order_by(lg.c.id)
        if source_name is not None:
            stmt = stmt.where(lg.c.source_name == source_name)
        if source_col is not None:
            stmt = stmt.where(lg.c.source_col == source_col)
        return [_entry(r) for r in self._read(stmt)]

    def approve(
        self,
        item_id: int,
        actor: str,
        note: str | None = None,
        target: str | None = None,
        now: datetime | None = None,
    ) -> ReviewItem:
        """Approve a pending item, optionally correcting the target to a glossary field."""
        if target is not None:
            if self._fields is None:
                raise MappingError("correction de cible impossible sans dictionnaire sémantique")
            if target not in self._fields:
                raise MappingError("cible corrigée absente du glossaire canonique")
        return self._decide(item_id, "approved", "approve", _actor(actor), _note(note), target, now)

    def reject(
        self, item_id: int, actor: str, note: str | None = None, now: datetime | None = None
    ) -> ReviewItem:
        """Reject a pending item."""
        return self._decide(item_id, "rejected", "reject", _actor(actor), _note(note), None, now)

    def _decide(
        self,
        item_id: int,
        status: Status,
        action: LogAction,
        actor: str,
        note: str | None,
        target: str | None,
        now: datetime | None,
    ) -> ReviewItem:
        q, ts = review_queue, _utc(now)
        try:
            with self._engine.begin() as conn:
                res = conn.execute(
                    update(q).where(q.c.id == item_id, q.c.status == "pending").values(
                        status=status, decided_at=ts, decided_by=actor, decision_note=note,
                        final_target=(target or q.c.target) if status == "approved" else None,
                    )
                )  # fmt: skip
                row = conn.execute(select(q).where(q.c.id == item_id)).first()
                if row is None:
                    raise MappingError(f"élément de revue inconnu : {item_id}")
                if res.rowcount != 1:
                    raise MappingError(f"élément {item_id} déjà décidé (statut {row.status})")
                conn.execute(insert(mapping_decision_log).values(_log_values(
                    row, target=row.final_target or row.target, queue_id=item_id,
                    source_name=row.source_name, action=action, actor=actor,
                    reason=row.reason, decided_at=ts, note=note,
                )))  # fmt: skip
        except SQLAlchemyError as e:
            raise MappingError(f"décision impossible à enregistrer ({type(e).__name__})") from e
        log.info("mapping_review_decided", item_id=item_id, action=action, actor=actor)
        return _item(row)

    def revert(
        self, log_id: int, actor: str, note: str | None = None, now: datetime | None = None
    ) -> ReviewItem:
        """Revoke the latest decision of a column: journal 'revert', item back to pending."""
        who, why, ts = _actor(actor), _note(note), _utc(now)
        q, lg = review_queue, mapping_decision_log
        try:
            with self._engine.begin() as conn:
                e = conn.execute(select(lg).where(lg.c.id == log_id)).first()
                if e is None:
                    raise MappingError(f"décision inconnue : {log_id}")
                if e.action not in ("auto_apply", "approve", "reject") or e.target is None:
                    raise MappingError(f"décision {log_id} non révocable ({e.action})")
                last = conn.execute(
                    select(lg.c.id).where(
                        lg.c.source_name == e.source_name, lg.c.source_col == e.source_col,
                        lg.c.action.in_(_DECISIONS),
                    ).order_by(lg.c.id.desc()).limit(1)
                ).scalar()  # fmt: skip
                if last != e.id:
                    raise MappingError(f"décision {log_id} n'est plus la dernière de la colonne")
                reset = {"status": "pending", "decided_at": None, "decided_by": None,
                         "decision_note": None, "final_target": None}  # fmt: skip
                if e.queue_id is not None:
                    qid = e.queue_id
                    res = conn.execute(
                        update(q).where(q.c.id == qid, q.c.status.in_(("approved", "rejected")))
                        .values(**reset)
                    )  # fmt: skip
                    if res.rowcount != 1:
                        raise MappingError(f"décision {log_id} déjà révoquée")
                else:
                    key = (
                        q.c.source_name == e.source_name,
                        q.c.source_col == e.source_col,
                        q.c.target == e.target,
                        q.c.prompt_version == e.prompt_version,
                    )
                    existing = conn.execute(select(q.c.id).where(*key)).scalar()
                    if existing is not None:
                        conn.execute(update(q).where(q.c.id == existing).values(**reset))
                        qid = existing
                    else:
                        qid = conn.execute(insert(q).returning(q.c.id).values(
                            source_name=e.source_name, source_col=e.source_col, target=e.target,
                            score=e.score, method=e.method, origin=e.origin, model=e.model,
                            prompt_version=e.prompt_version, rationale="", reason="reverted",
                            status="pending", created_at=ts,
                        )).scalar_one()  # fmt: skip
                conn.execute(insert(lg).values(_log_values(
                    e, queue_id=qid, source_name=e.source_name, action="revert", actor=who,
                    reason=e.action, decided_at=ts, note=why,
                )))  # fmt: skip
                row = conn.execute(select(q).where(q.c.id == qid)).one()
        except SQLAlchemyError as exc:
            raise MappingError(
                f"révocation impossible à enregistrer ({type(exc).__name__})"
            ) from exc
        log.info("mapping_decision_reverted", log_id=log_id, item_id=qid, actor=who)
        return _item(row)

    def _read(self, stmt: Any) -> list[Row[Any]]:
        try:
            with self._engine.connect() as conn:
                return list(conn.execute(stmt))
        except SQLAlchemyError as e:
            raise MappingError(
                f"lecture de la file de revue impossible ({type(e).__name__})"
            ) from e

    def list(self, status: str | None = None, source_name: str | None = None) -> list[ReviewItem]:
        """Queue rows by id, optionally filtered by status and source.

        Defined last: the name shadows the builtin `list` in the class body annotations.
        """
        q = review_queue
        stmt = select(q).order_by(q.c.id)
        if status is not None:
            if status not in STATUSES:
                raise MappingError(f"statut inconnu (attendu : {', '.join(sorted(STATUSES))})")
            stmt = stmt.where(q.c.status == status)
        if source_name is not None:
            stmt = stmt.where(q.c.source_name == _check_source(source_name))
        return [_item(r) for r in self._read(stmt)]
