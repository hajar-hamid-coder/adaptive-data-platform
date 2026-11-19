"""Quality score (P4-03): per column and per batch, 0-100, from a P4-01 `QualityReport`.

Pure and deterministic (no clock, no I/O in `score_batch`). Scores hold ratios and counts only,
never data values (no min/max, no statistics of the data).

Dimensions, computed PER COLUMN:
- completeness: share of non-null cells, computed on the rows. Only REQUIRED columns count:
  `ScoreConfig.required_columns` if given (even empty), else the columns of the `not_null`
  rules of the YAML. An optional column is not penalised, its rate is still reported.
- validity: `range`/`regex`/`reference` rules of the column, each = share of evaluated (non-null)
  rows that pass; combined as a mean weighted by severity (error 1.0, warn `warn_weight`).
- uniqueness: `unique` rules whose key contains the column (rows not repeated); n/a otherwise.
- freshness: the `freshness` rule of the column: 100 if within `max_age_days`, then linear decay
  to 0 at 2x the limit (age 0 limit: any age > 0 scores 0; no usable date: 0).
A dimension without applicable rule (absent column, `skipped`, nothing evaluated) is n/a: it is
EXCLUDED and the remaining weights are renormalised, so a set without freshness rule can be 100.

Column score = weighted mean of its applicable dimensions (`weights`). Batch score = mean of the
column scores weighted by `column_weights` (default 1, e.g. weight keys higher); batch dimensions
are the same column-weighted means (completeness over required columns only). Empty batch (no row,
no column) or nothing scorable -> status "n/a", score None. Rounded to 6 decimals.
Freshness is read from the report (evaluated with `as_of` by `evaluate`); `as_of` of `score_batch`
is accepted for API symmetry only.
`rows_failed`: batch = rows failing an `error` row rule; column = lower bound (largest failure
count of one `error` rule on the column, or its null count if required).
"""

import hashlib
import json
import math
import re
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
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
)
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from adp.errors import ConfigError, QualityError
from adp.logging import get_logger
from adp.mapping.dictionary import SemanticDictionary
from adp.mapping.transform import Value
from adp.quality.rules import (
    Freshness,
    NotNull,
    QualityReport,
    Rule,
    RuleResult,
    Unique,
)
from adp.schema.registry import _check_source, metadata

log = get_logger(__name__)

DIMENSIONS = ("completeness", "validity", "uniqueness", "freshness")
_NAME = r"^[a-z][a-z0-9_]*$"


class Weights(BaseModel):
    """Dimension weights (>= 0, finite, sum > 0); renormalised over applicable dimensions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    completeness: float = Field(default=0.35, ge=0, le=1000, allow_inf_nan=False)
    validity: float = Field(default=0.35, ge=0, le=1000, allow_inf_nan=False)
    uniqueness: float = Field(default=0.20, ge=0, le=1000, allow_inf_nan=False)
    freshness: float = Field(default=0.10, ge=0, le=1000, allow_inf_nan=False)

    @model_validator(mode="after")
    def _positive_sum(self) -> Self:
        if sum(self.model_dump().values()) <= 0:
            raise ValueError("dimension weights must have a positive sum")
        return self


class ScoreConfig(BaseModel):
    """Weighting of the score. `required_columns=None` -> deduced from the `not_null` rules."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    weights: Weights = Weights()
    warn_weight: float = Field(default=0.25, ge=0, le=1, allow_inf_nan=False)
    required_columns: list[str] | None = Field(default=None, max_length=500)
    column_weights: dict[str, float] = Field(default_factory=dict, max_length=500)

    @field_validator("required_columns")
    @classmethod
    def _names(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and any(not re.fullmatch(_NAME, c) for c in v):
            raise ValueError("column names must match " + _NAME)
        return v

    @field_validator("column_weights")
    @classmethod
    def _col_weights(cls, v: dict[str, float]) -> dict[str, float]:
        for k, w in v.items():
            if not re.fullmatch(_NAME, k):
                raise ValueError("column names must match " + _NAME)
            if not math.isfinite(w) or not 0 <= w <= 1000:
                raise ValueError(f"weight of column '{k}' must be in [0, 1000]")
        return v


class ColumnScore(BaseModel):
    """Score of one column; a None dimension is n/a (excluded)."""

    model_config = ConfigDict(frozen=True)

    column: str
    required: bool
    completeness: float | None  # reported for optional columns too, but not scored
    validity: float | None
    uniqueness: float | None
    freshness: float | None
    score: float | None
    rows_failed: int


class BatchScore(BaseModel):
    """Score of a batch and of its columns. `status` "n/a" when nothing is scorable."""

    model_config = ConfigDict(frozen=True)

    status: Literal["scored", "n/a"]
    score: float | None
    completeness: float | None
    validity: float | None
    uniqueness: float | None
    freshness: float | None
    rows_total: int
    rows_failed: int
    columns: list[ColumnScore]
    config_hash: str


def load_score_config(path: Path, dictionary: SemanticDictionary | None = None) -> ScoreConfig:
    """Load config/quality/score.yaml; errors name the file and field (no data value)."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: cannot read YAML ({type(exc).__name__})") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a YAML mapping")
    try:
        cfg = ScoreConfig.model_validate(raw)
    except ValidationError as exc:
        why = "; ".join(
            f"field '{'.'.join(str(p) for p in e['loc']) or '?'}': {e['msg']}" for e in exc.errors()
        )
        raise ConfigError(f"{path}: invalid score config: {why}") from exc
    if dictionary is not None:
        known = {f.name for f in dictionary.fields}
        for field, cols in (
            ("required_columns", cfg.required_columns or []),
            ("column_weights", list(cfg.column_weights)),
        ):
            for c in cols:
                if c not in known:
                    raise ConfigError(f"{path}: field '{field}': column '{c}' not in the glossary")
    return cfg


def _wmean(pairs: Sequence[tuple[float, float | None]]) -> float | None:
    """Mean of values weighted; n/a (None) values and zero weights are excluded."""
    used = [(w, v) for w, v in pairs if v is not None and w > 0]
    total = sum(w for w, _ in used)
    if not used or total <= 0:
        return None
    return _r(sum(w * v for w, v in used) / total)


def _r(x: float) -> float:
    return round(x, 6)


def _share(r: RuleResult) -> float | None:
    """Pass share (0-100) of a row rule, None if not evaluated."""
    if r.status == "skipped" or r.evaluated <= 0:
        return None
    return 100.0 * (r.evaluated - r.failed_count) / r.evaluated


def _fresh(rule: Freshness, r: RuleResult) -> float | None:
    if r.status == "skipped":
        return None
    if r.status == "passed":
        return 100.0
    age, limit = r.detail.get("age_days"), rule.max_age_days
    if not isinstance(age, int | float) or limit <= 0:
        return 0.0
    return max(0.0, 100.0 * (2 * limit - age) / limit)


def _config_hash(cfg: ScoreConfig, required: Sequence[str]) -> str:
    payload = {
        "weights": cfg.weights.model_dump(), "warn_weight": cfg.warn_weight,
        "required": sorted(required), "column_weights": dict(sorted(cfg.column_weights.items())),
    }  # fmt: skip
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def score_batch(
    columns: Sequence[str],
    rows: Sequence[Sequence[Value]],
    report: QualityReport,
    rules: Sequence[Rule],
    *,
    config: ScoreConfig = ScoreConfig(),  # noqa: B008
    as_of: date | datetime | None = None,  # noqa: ARG001 - freshness is read from the report
) -> BatchScore:
    """Score a batch from its quality report. Never raises on data."""
    n = len(rows)
    width = len(columns)
    if config.required_columns is not None:
        required = set(config.required_columns)
    else:
        required = {r.column for r in rules if isinstance(r, NotNull)}
    by_id = {r.rule_id: r for r in report.results}
    w = config.weights
    dim_w = {d: getattr(w, d) for d in DIMENSIONS}

    scored: list[ColumnScore] = []
    for j, name in enumerate(columns):
        cells = [r[j] if len(r) == width else None for r in rows]
        nulls = sum(v is None for v in cells)
        comp = _r(100.0 * (n - nulls) / n) if n else None
        valid: list[tuple[float, float | None]] = []
        uniq: list[tuple[float, float | None]] = []
        fresh: list[tuple[float, float | None]] = []
        failed = nulls if name in required else 0
        for rule in rules:
            res = by_id.get(rule.id)
            if res is None:
                continue
            sev = 1.0 if rule.severity == "error" else config.warn_weight
            if isinstance(rule, Unique):
                if name in rule.columns:
                    uniq.append((sev, _share(res)))
            elif isinstance(rule, NotNull):
                pass  # covered by completeness, computed on the rows
            elif rule.column != name:
                continue
            elif isinstance(rule, Freshness):
                fresh.append((sev, _fresh(rule, res)))
            else:
                valid.append((sev, _share(res)))
            if rule.severity == "error" and res.level == "row" and (
                isinstance(rule, Unique) and name in rule.columns
                or not isinstance(rule, Unique | NotNull) and rule.column == name
            ):  # fmt: skip
                failed = max(failed, res.failed_count)
        dims: dict[str, float | None] = {
            "completeness": comp if name in required else None,
            "validity": _wmean(valid),
            "uniqueness": _wmean(uniq),
            "freshness": _wmean(fresh),
        }
        score = _wmean([(dim_w[d], dims[d]) for d in DIMENSIONS]) if n else None
        scored.append(ColumnScore(
            column=name, required=name in required, completeness=comp,
            validity=dims["validity"], uniqueness=dims["uniqueness"],
            freshness=dims["freshness"], score=score, rows_failed=failed,
        ))  # fmt: skip

    cw = {c.column: config.column_weights.get(c.column, 1.0) for c in scored}

    def agg(dim: str, only_required: bool = False) -> float | None:
        return _wmean([
            (cw[c.column], getattr(c, dim)) for c in scored if c.required or not only_required
        ])  # fmt: skip

    total = _wmean([(cw[c.column], c.score) for c in scored])
    return BatchScore(
        status="scored" if total is not None else "n/a",
        score=total,
        completeness=agg("completeness", only_required=True),
        validity=agg("validity"),
        uniqueness=agg("uniqueness"),
        freshness=agg("freshness"),
        rows_total=n,
        rows_failed=len(report.failed_row_indices),
        columns=scored,
        config_hash=_config_hash(config, sorted(required)),
    )


# --- persistence ---------------------------------------------------------------------------
# DDL owned by Alembic revision 0004. Append-only: the store exposes no update/delete.
# `column_name` is '' for the batch row (a NULL would escape the unique constraint on
# PostgreSQL and SQLite, and idempotence must hold under concurrency).
_MAX_RETRIES = 5
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_VALUES = (
    "schema_version", "completeness", "validity", "uniqueness", "freshness", "score",
    "rows_total", "rows_failed", "config_hash",
)  # fmt: skip

quality_score = Table(
    "quality_score",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source_name", String(255), nullable=False),
    Column("run_id", String(64), nullable=False),
    Column("schema_version", Integer, nullable=True),
    Column("scope", String(8), nullable=False),
    Column("column_name", String(255), nullable=False),
    Column("completeness", Float, nullable=True),
    Column("validity", Float, nullable=True),
    Column("uniqueness", Float, nullable=True),
    Column("freshness", Float, nullable=True),
    Column("score", Float, nullable=True),
    Column("rows_total", Integer, nullable=False),
    Column("rows_failed", Integer, nullable=False),
    Column("config_hash", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("source_name", "run_id", "scope", "column_name", name="uq_quality_score_run"),
)


class StoredScore(BaseModel):
    """One persisted row (`column_name` None for the batch scope)."""

    model_config = ConfigDict(frozen=True)

    id: int
    source_name: str
    run_id: str
    schema_version: int | None
    scope: Literal["batch", "column"]
    column_name: str | None
    completeness: float | None
    validity: float | None
    uniqueness: float | None
    freshness: float | None
    score: float | None
    rows_total: int
    rows_failed: int
    config_hash: str
    created_at: datetime


def _stored(row: Row) -> StoredScore:  # type: ignore[type-arg]
    d = row._asdict()
    d["column_name"] = d["column_name"] or None
    ts = d["created_at"]
    d["created_at"] = ts if ts.tzinfo else ts.replace(tzinfo=UTC)  # SQLite drops the zone
    return StoredScore.model_validate(d)


def _rows(
    source_name: str, run_id: str, b: BatchScore, schema_version: int | None
) -> dict[tuple[str, str], dict[str, object]]:
    base = {"source_name": source_name, "run_id": run_id, "schema_version": schema_version}
    out: dict[tuple[str, str], dict[str, object]] = {
        ("batch", ""): base
        | {
            "scope": "batch",
            "column_name": "",
            "completeness": b.completeness,
            "validity": b.validity,
            "uniqueness": b.uniqueness,
            "freshness": b.freshness,
            "score": b.score,
            "rows_total": b.rows_total,
            "rows_failed": b.rows_failed,
            "config_hash": b.config_hash,
        }  # fmt: skip
    }
    for c in b.columns:
        out[("column", c.column)] = base | {
            "scope": "column", "column_name": c.column, "completeness": c.completeness,
            "validity": c.validity, "uniqueness": c.uniqueness, "freshness": c.freshness,
            "score": c.score, "rows_total": b.rows_total, "rows_failed": c.rows_failed,
            "config_hash": b.config_hash,
        }  # fmt: skip
    return out


class QualityScoreStore:
    """Append-only, idempotent persistence of scores per (source, run). Raises QualityError."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def save(
        self,
        source_name: str,
        run_id: str,
        batch_score: BatchScore,
        schema_version: int | None = None,
        now: datetime | None = None,
    ) -> int:
        """Persist the batch row and one row per column in ONE transaction.

        Replaying the same run with identical scores is a no-op (returns 0); a different score
        for an existing run raises QualityError (history is never rewritten). Returns the
        number of rows inserted.
        """
        try:
            _check_source(source_name)
        except Exception as e:
            raise QualityError("nom de source invalide") from e
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise QualityError("run_id invalide (1 à 64 caractères [A-Za-z0-9._:-])")
        new = _rows(source_name, run_id, batch_score, schema_version)
        ts = now if now is not None else datetime.now(UTC)
        ts = ts if ts.tzinfo else ts.replace(tzinfo=UTC)
        t = quality_score
        for _ in range(_MAX_RETRIES):
            try:
                with self._engine.begin() as conn:
                    old = {
                        (r.scope, r.column_name): r._asdict()
                        for r in conn.execute(
                            select(t).where(t.c.source_name == source_name, t.c.run_id == run_id)
                        )
                    }
                    if old:
                        same = old.keys() == new.keys() and all(
                            all(old[k][f] == new[k][f] for f in _VALUES) for k in new
                        )
                        if not same:
                            raise QualityError(
                                f"{source_name}/{run_id} : scores déjà enregistrés et différents"
                            )
                        return 0
                    conn.execute(insert(t), [v | {"created_at": ts} for v in new.values()])
                break
            except IntegrityError:
                continue  # a concurrent save of the same run won: replay (compares, no-op)
            except SQLAlchemyError as e:
                raise QualityError(f"écriture du score impossible ({type(e).__name__})") from e
        else:
            raise QualityError(f"{source_name}/{run_id} : écriture abandonnée après conflits")
        log.info(
            "quality_score_saved", source_name=source_name, run_id=run_id,
            score=batch_score.score, rows=batch_score.rows_total,
        )  # fmt: skip
        return len(new)

    def _read(self, source_name: str, scope: str, column: str, limit: int) -> list[StoredScore]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise QualityError("limit doit être un entier entre 1 et 1000")
        t = quality_score
        stmt = (
            select(t)
            .where(
                t.c.source_name == _check_source(source_name),
                t.c.scope == scope,
                t.c.column_name == column,
            )
            .order_by(t.c.created_at.desc(), t.c.id.desc())
            .limit(limit)
        )
        try:
            with self._engine.connect() as conn:
                return [_stored(r) for r in conn.execute(stmt)]
        except SQLAlchemyError as e:
            raise QualityError(f"lecture du score impossible ({type(e).__name__})") from e

    def latest(self, source_name: str) -> StoredScore | None:
        """Most recent batch score of a source, or None."""
        rows = self._read(source_name, "batch", "", 1)
        return rows[0] if rows else None

    def history(self, source_name: str, limit: int = 50) -> list[StoredScore]:
        """Batch scores, newest first."""
        return self._read(source_name, "batch", "", limit)

    def column_history(
        self, source_name: str, column_name: str, limit: int = 50
    ) -> list[StoredScore]:
        """Scores of one column, newest first."""
        if not isinstance(column_name, str) or not 1 <= len(column_name) <= 255:
            raise QualityError("nom de colonne invalide")
        return self._read(source_name, "column", column_name, limit)
