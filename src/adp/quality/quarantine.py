"""Dynamic quarantine (P4-04): isolate failing rows or whole batches, release, replay.

Decision (`decide`, pure), thresholds from config/quality/quarantine.yaml (H-02, provisional):
- failed rate = rows failing an `error` row rule / rows (warn rules never quarantine);
- rate < `alert_rate` (5 %): row quarantine, the rest passes;
- `alert_rate` <= rate < `batch_rate` (5-20 %): row quarantine too, the rest passes, but the
  decision carries `alert=True` (degraded batch to look at; the pipeline is not blocked);
- rate >= `batch_rate` (20 %), batch score < `critical_score`, or a failed `error` batch-level
  rule (freshness): the WHOLE batch is isolated, nothing passes.

Storage (`QuarantineStore`, any write-once `ObjectStore`, no database):
- `<source>/<batch_id>/record.json`: the isolated rows (values live in the store only), their
  reasons (rule_id, severity), the level. Written once; the same batch again is a no-op, a
  different content for the same id raises QualityError (never rewritten).
- `<source>/<batch_id>/events/<seq>.json`: append-only journal of `release` / `replay` events.
  Row status (quarantined -> released | replayed) is derived from the journal; a row leaves at
  most once (state re-read and optimistic `put_if_absent` on the next sequence number, retried
  on conflict), so concurrent releases/replays never duplicate a row.
- `release`: manual exit, author and reason mandatory. `replay`: re-evaluate the remaining rows
  (new rules and/or corrected rows) and let out only those that pass now; for an isolated batch
  the batch thresholds are applied again to the remaining rows first.
Limits: replay evaluates the remaining rows on their own (a `unique` rule does not see the rows
that already passed); the score threshold is applied at intake only. Logs carry ids, counts and
rule ids, never values; identifiers are validated (no path traversal).
"""

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from adp.bronze.store import ObjectStore
from adp.errors import ConfigError, QualityError
from adp.logging import get_logger
from adp.mapping.transform import Value
from adp.quality.rules import QualityReport, RefValue, Rule, evaluate
from adp.quality.score import BatchScore
from adp.schema.registry import _check_source

log = get_logger(__name__)

Level = Literal["none", "row", "batch"]
RowStatus = Literal["quarantined", "released", "replayed"]
_BATCH_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_RETRIES = 20
_TEXT_MAX = 500


class QuarantineConfig(BaseModel):
    """Thresholds (H-02). `critical_score` None disables the score check."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    alert_rate: float = Field(default=0.05, gt=0, le=1, allow_inf_nan=False)
    batch_rate: float = Field(default=0.20, gt=0, le=1, allow_inf_nan=False)
    critical_score: float | None = Field(default=60.0, ge=0, le=100, allow_inf_nan=False)

    @model_validator(mode="after")
    def _order(self) -> Self:
        if self.alert_rate > self.batch_rate:
            raise ValueError("alert_rate must be <= batch_rate")
        return self


def load_quarantine_config(path: Path) -> QuarantineConfig:
    """Load config/quality/quarantine.yaml; errors name the file and field (no data value)."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: cannot read YAML ({type(exc).__name__})") from exc
    if not isinstance(raw, dict | None):
        raise ConfigError(f"{path}: expected a YAML mapping")
    try:
        return QuarantineConfig.model_validate(raw or {})
    except ValidationError as exc:
        why = "; ".join(f"field '{'.'.join(map(str, e['loc'])) or '?'}': {e['msg']}"
                        for e in exc.errors())  # fmt: skip
        raise ConfigError(f"{path}: invalid quarantine config: {why}") from exc


class Decision(BaseModel):
    """What to isolate. `quarantine_rows`: all rows (batch), failing rows (row) or none."""

    model_config = ConfigDict(frozen=True)

    level: Level
    alert: bool
    failed_rate: float
    batch_reasons: list[str]
    quarantine_rows: list[int]


def decide(
    report: QualityReport, config: QuarantineConfig, score: BatchScore | None = None
) -> Decision:
    """Pure decision from a P4-01 report (and optionally a P4-03 score)."""
    n = report.rows_total
    failed = report.failed_row_indices
    rate = len(failed) / n if n else 0.0
    reasons: list[str] = []
    if n and rate >= config.batch_rate:
        reasons.append(f"failed_rate={rate:.4f}>={config.batch_rate}")
    if (
        config.critical_score is not None and score is not None and score.score is not None
        and score.score < config.critical_score
    ):  # fmt: skip
        reasons.append(f"score={score.score}<{config.critical_score}")
    reasons += [
        f"rule:{r.rule_id}"
        for r in report.results
        if r.level == "batch" and r.severity == "error" and r.status == "failed"
    ]
    if reasons:
        return Decision(level="batch", alert=True, failed_rate=rate, batch_reasons=reasons,
                        quarantine_rows=list(range(n)))  # fmt: skip
    level: Level = "row" if failed else "none"
    return Decision(level=level, alert=rate >= config.alert_rate, failed_rate=rate,
                    batch_reasons=[], quarantine_rows=list(failed))  # fmt: skip


# --- value encoding (JSON with tagged dates) -----------------------------------------------


def _enc(v: Value) -> Any:
    if isinstance(v, datetime):
        return {"$datetime": v.isoformat()}
    if isinstance(v, date):
        return {"$date": v.isoformat()}
    return v


def _dec(v: Any) -> Value:
    if isinstance(v, dict):
        if "$datetime" in v:
            return datetime.fromisoformat(v["$datetime"])
        return date.fromisoformat(v["$date"])
    return v  # type: ignore[no-any-return]


def _dump(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()


# --- models of the stored state ------------------------------------------------------------


@dataclass(frozen=True)
class QuarantinedRow:
    """One isolated row; `values` are the corrected ones once replayed with a correction."""

    row: int
    values: list[Value]
    reasons: list[tuple[str, str]]
    status: RowStatus


@dataclass(frozen=True)
class QuarantineState:
    source: str
    batch_id: str
    level: Level
    alert: bool
    batch_reasons: list[str]
    columns: list[str]
    rows: list[QuarantinedRow]
    counts: dict[str, int]

    @property
    def status(self) -> Literal["quarantined", "released", "replayed", "mixed"]:
        if self.counts["quarantined"]:
            return "quarantined"
        if not self.counts["replayed"]:
            return "released"
        return "replayed" if not self.counts["released"] else "mixed"


@dataclass(frozen=True)
class ApplyOutcome:
    decision: Decision
    passed: list[Sequence[Value]]
    quarantined: int


@dataclass(frozen=True)
class ReplayOutcome:
    passed: list[tuple[int, list[Value]]]
    still_quarantined: int


# --- store ---------------------------------------------------------------------------------


def _ids(source: str, batch_id: str) -> str:
    try:
        _check_source(source)
    except Exception as e:
        raise QualityError("nom de source invalide") from e
    if not isinstance(batch_id, str) or not _BATCH_ID.fullmatch(batch_id) or ".." in batch_id:
        raise QualityError("identifiant de lot invalide (1 à 128 caractères [A-Za-z0-9._-])")
    return f"{source}/{batch_id}"


def _text(value: str, what: str, limit: int) -> str:
    v = (value or "").strip()
    if not v or len(v) > limit or not v.isprintable():
        raise QualityError(f"{what} obligatoire (1 à {limit} caractères imprimables)")
    return v


def _ts(now: datetime | None) -> str:
    ts = now or datetime.now(UTC)
    return (ts if ts.tzinfo else ts.replace(tzinfo=UTC)).isoformat()


class QuarantineStore:
    """Quarantine on a write-once object store. Raises QualityError."""

    def __init__(self, store: ObjectStore) -> None:
        self._store = store

    def apply(
        self,
        source: str,
        batch_id: str,
        columns: Sequence[str],
        rows: Sequence[Sequence[Value]],
        report: QualityReport,
        *,
        config: QuarantineConfig = QuarantineConfig(),  # noqa: B008
        score: BatchScore | None = None,
        run_id: str,
        now: datetime | None = None,
    ) -> ApplyOutcome:
        """Decide, store the isolated rows (if any) and return the rows that pass."""
        base = _ids(source, batch_id)
        if report.rows_total != len(rows):
            raise QualityError("le rapport qualité ne correspond pas au lot")
        d = decide(report, config, score)
        isolated = set(d.quarantine_rows)
        passed = [r for i, r in enumerate(rows) if i not in isolated]
        if not isolated:
            return ApplyOutcome(d, passed, 0)
        reasons: dict[int, list[list[str]]] = {i: [] for i in isolated}
        by_rule: Counter[str] = Counter()
        for res in report.results:
            if res.level == "row" and res.severity == "error" and res.failed_count:
                for i in res.failed_rows:
                    reasons[i].append([res.rule_id, res.severity])
                by_rule[res.rule_id] += res.failed_count
        content = {
            "source": source, "batch_id": batch_id, "level": d.level, "alert": d.alert,
            "failed_rate": d.failed_rate, "batch_reasons": d.batch_reasons,
            "columns": list(columns),
            "rows": [{"row": i, "values": [_enc(v) for v in rows[i]], "reasons": reasons[i]}
                     for i in sorted(isolated)],
        }  # fmt: skip
        digest = hashlib.sha256(_dump(content)).hexdigest()
        record = content | {"content_hash": digest, "run_id": run_id, "created_at": _ts(now)}
        key = f"{base}/record.json"
        if not self._store.put_if_absent(key, _dump(record)):
            if self._record(base).get("content_hash") != digest:
                raise QualityError(f"{base} : lot déjà en quarantaine avec un autre contenu")
        log.info("quarantine_applied", source=source, batch_id=batch_id, run_id=run_id,
                 level=d.level, alert=d.alert, rows_total=len(rows), quarantined=len(isolated),
                 by_rule=dict(by_rule), batch_reasons=d.batch_reasons)  # fmt: skip
        return ApplyOutcome(d, passed, len(isolated))

    def _record(self, base: str) -> dict[str, Any]:
        key = f"{base}/record.json"
        if not self._store.exists(key):
            raise QualityError(f"{base} : aucun lot en quarantaine")
        return json.loads(self._store.get(key))  # type: ignore[no-any-return]

    def _events(self, base: str) -> list[dict[str, Any]]:
        keys = self._store.list(f"{base}/events/")
        return [json.loads(self._store.get(k)) for k in keys]

    def _state(self, source: str, batch_id: str) -> tuple[QuarantineState, int]:
        base = _ids(source, batch_id)
        rec = self._record(base)
        events = self._events(base)
        status: dict[int, RowStatus] = {}
        corrected: dict[int, list[Value]] = {}
        for ev in events:
            for i in ev["rows"]:
                status[i] = "released" if ev["action"] == "release" else "replayed"
            for i, vals in ev.get("corrected", {}).items():
                corrected[int(i)] = [_dec(v) for v in vals]
        rows = [
            QuarantinedRow(
                row=r["row"],
                values=corrected.get(r["row"], [_dec(v) for v in r["values"]]),
                reasons=[(a, b) for a, b in r["reasons"]],
                status=status.get(r["row"], "quarantined"),
            )
            for r in rec["rows"]
        ]
        counts = {"quarantined": 0, "released": 0, "replayed": 0} | dict(
            Counter(r.status for r in rows)
        )
        state = QuarantineState(source, batch_id, rec["level"], rec["alert"],
                                rec["batch_reasons"], rec["columns"], rows, counts)  # fmt: skip
        return state, len(events)

    def state(self, source: str, batch_id: str) -> QuarantineState:
        """Current state, derived from the record and the journal."""
        return self._state(source, batch_id)[0]

    def list_batches(self, source: str) -> list[str]:
        """Batch ids in quarantine (any status) for a source."""
        try:
            _check_source(source)
        except Exception as e:
            raise QualityError("nom de source invalide") from e
        suffix = "/record.json"
        return [k[len(source) + 1 : -len(suffix)] for k in self._store.list(f"{source}/")
                if k.endswith(suffix) and k.count("/") == 2]  # fmt: skip

    def released_rows(
        self, source: str, batch_id: str
    ) -> tuple[list[str], list[tuple[int, list[Value]]]]:  # noqa: E501
        """Columns and (row index, values) of the rows released or replayed, for reloading."""
        st = self.state(source, batch_id)
        return st.columns, [(r.row, r.values) for r in st.rows if r.status != "quarantined"]

    def _append(self, source: str, batch_id: str, build: Any) -> Any:
        """Optimistic append: `build(state)` -> (event | None, result); retried on conflict."""
        base = _ids(source, batch_id)
        for _ in range(_MAX_RETRIES):
            state, seq = self._state(source, batch_id)
            event, result = build(state)
            if event is None:
                return result
            if self._store.put_if_absent(
                f"{base}/events/{seq:06d}.json", _dump(event | {"seq": seq})
            ):
                return result
        raise QualityError(f"{base} : écriture du journal abandonnée après conflits")

    def release(
        self,
        source: str,
        batch_id: str,
        *,
        by: str,
        reason: str,
        rows: Sequence[int] | None = None,
        run_id: str = "manual",
        now: datetime | None = None,
    ) -> list[int]:
        """Manually release `rows` (default: all remaining). Returns the rows released now."""
        who = _text(by, "auteur", 128)
        why = _text(reason, "raison", _TEXT_MAX)

        def build(state: QuarantineState) -> tuple[dict[str, Any] | None, list[int]]:
            remaining = {r.row for r in state.rows if r.status == "quarantined"}
            if rows is None:
                out = sorted(remaining)
            else:
                known = {r.row for r in state.rows}
                if any(i not in known for i in rows):
                    raise QualityError("ligne inconnue dans ce lot")
                out = sorted(set(rows) & remaining)
            if not out:
                return None, []
            return {"action": "release", "rows": out, "by": who, "reason": why,
                    "run_id": run_id, "at": _ts(now)}, out  # fmt: skip

        out: list[int] = self._append(source, batch_id, build)
        log.info("quarantine_released", source=source, batch_id=batch_id, run_id=run_id,
                 by=who, rows=len(out))  # fmt: skip
        return out

    def replay(
        self,
        source: str,
        batch_id: str,
        rules: Sequence[Rule],
        *,
        by: str,
        reason: str = "replay",
        corrections: Mapping[int, Sequence[Value]] | None = None,
        references: Mapping[str, Sequence[RefValue]] | None = None,
        as_of: date | datetime | None = None,
        config: QuarantineConfig = QuarantineConfig(),  # noqa: B008
        run_id: str = "manual",
        now: datetime | None = None,
    ) -> ReplayOutcome:
        """Re-evaluate the remaining rows; only those passing now leave (status replayed)."""
        who = _text(by, "auteur", 128)
        why = _text(reason, "raison", _TEXT_MAX)
        fixes = dict(corrections or {})

        def build(state: QuarantineState) -> tuple[dict[str, Any] | None, ReplayOutcome]:
            remaining = [r for r in state.rows if r.status == "quarantined"]
            ids = {r.row for r in remaining}
            for i, vals in fixes.items():
                if i not in ids:
                    raise QualityError("correction sur une ligne qui n'est pas en quarantaine")
                if len(vals) != len(state.columns):
                    raise QualityError("correction de largeur incorrecte")
            values = [list(fixes.get(r.row, r.values)) for r in remaining]
            rep = evaluate(rules, state.columns, values, references=references, as_of=as_of)
            d = decide(rep, config)
            # A failed batch-level rule blocks everything; rate thresholds only matter for an
            # isolated batch (a row-level subset is 100 % "failing" by construction).
            batch_rule = any(x.startswith("rule:") for x in d.batch_reasons)
            if batch_rule or (state.level == "batch" and d.level == "batch"):
                bad = set(range(len(values)))
            else:
                bad = set(rep.failed_row_indices)
            passed = [(remaining[k].row, values[k]) for k in range(len(values)) if k not in bad]
            result = ReplayOutcome(passed, len(remaining) - len(passed))
            if not passed:
                return None, result
            event = {
                "action": "replay", "rows": [i for i, _ in passed], "by": who, "reason": why,
                "run_id": run_id, "at": _ts(now), "rules": sorted(r.id for r in rules),
                "corrected": {str(i): [_enc(v) for v in v_] for i, v_ in passed if i in fixes},
            }  # fmt: skip
            return event, result

        out: ReplayOutcome = self._append(source, batch_id, build)
        log.info("quarantine_replayed", source=source, batch_id=batch_id, run_id=run_id, by=who,
                 replayed=len(out.passed), still_quarantined=out.still_quarantined)  # fmt: skip
        return out
