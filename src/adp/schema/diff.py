"""Schema diff between two profiled schemas, with probable-rename detection (P2-03).

`diff_schemas(old, new)` is pure and deterministic (no I/O, no logging). It returns a
PROPOSAL: nothing is applied; renames carry a score and a decision for human review (H-02).

Algorithm
1. Columns with the same name are paired first (exact match, case-sensitive).
2. Rename candidates = every (dropped column x added column). Each pair gets
   `score = (wn*name_sim + wt*type_sim + wd*dist_sim) / (wn + wt + wd)`, all terms in [0, 1]:
   - name_sim: names are normalised (accents removed, camelCase and `_ - space` split into
     lower-case tokens). If both names collapse to the same compact string -> 1. Otherwise the
     tokens are soft-aligned (greedy, highest similarity first): equal = 1; abbreviation
     (short token >= 3 chars, same first letter, letters in order inside the long one: `tel` ->
     `telephone`, `qte` -> `quantite`) = `abbreviation_similarity`; initials of a run of tokens
     (`cp` -> `code postal`) = `abbreviation_similarity`; difflib ratio >= `token_fuzzy_min`
     (typos, plurals) = that ratio; else 0. Generic tokens (`id`, `date`, `taux`, `total`...)
     weigh `generic_token_weight` instead of 1, so a shared `date_` prefix is weak evidence.
     With cov_x = aligned weight / total weight on side x,
     `name_sim = max(cov) * sqrt(min(cov))`: extension (`population` -> `population_totale`:
     one side fully covered) stays high, while substitution (`prix_ht` -> `prix_ttc`: both
     sides keep an unexplained token) is strongly damped.
   - type_sim: `TYPE_SIMILARITY` (same type 1, integer/float and date/datetime 0.8, anything
     to string 0.2 ...). The type never blocks a rename (a rename can come with a type drift),
     it only lowers the score.
   - dist_sim: profile statistics only, never values (`examples` are ignored): weighted mean of
     null-rate closeness (1), distinct-ratio closeness (1) and value stats (2): numeric = mean
     of [min, max] interval IoU and relative closeness of means; string = IoU of the length
     intervals; same type without value stats (date, boolean) = not counted; incomparable kinds
     (numeric vs string) = 0.
3. One-to-one matching: candidates are sorted by (-score, old name, new name) and accepted
   greedily while both columns are free and score >= review_threshold. Greedy (not Hungarian)
   is deliberate: deterministic, explainable, and the scores of competing pairs are rarely
   close enough for the optimum to differ. decision = auto (>= auto_threshold), review, or
   rejected (then reported as REMOVED + ADDED).
   Ties: two candidates with the SAME score competing for the same old or new column are
   broken by alphabetical order (old name, then new name): deterministic but arbitrary. The
   scores and the decision are unchanged by the tie-break, so a tied pair below
   auto_threshold stays in `review`. The RENAMED change then carries `detail["tie"] = True`
   (key absent otherwise) so the review UI can show that another pairing scored the same.
4. Each paired column (same name or renamed) is compared: TYPE_CHANGED (type differs, format
   hints in detail), else FORMAT_CHANGED (format_hint differs: `1 234` vs `1234` is the same
   integer type but a different raw format to clean), and NULLABILITY_CHANGED. REORDERED is
   emitted once when the relative order of the paired columns changed.

Output order: by kind, then column name - independent of the input column order (except
REORDERED, which is about order by definition). `ChangeKind` values are the vocabulary of
`schema_change.change_type` and of the simulator's `expected_changes.json`.

Known limits: purely semantic renames with no lexical link (`revenue` -> `chiffre_affaires`)
score low by design (left to the P3 LLM mapping); an abbreviation of an unrelated word with a
compatible type (`nom` -> `nombre`) may reach `review`, never `auto`.
"""

import math
import re
import unicodedata
from collections.abc import Sequence
from difflib import SequenceMatcher
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from adp.errors import SchemaError
from adp.schema.profile import ColumnProfile, SchemaFingerprint
from adp.schema.registry import SchemaChange

Decision = Literal["auto", "review", "rejected"]

DEFAULT_GENERIC_TOKENS: frozenset[str] = frozenset(
    {
        "id", "code", "num", "no", "numero", "nb", "nbr", "nombre", "date", "dt", "taux",
        "pct", "total", "totale", "is", "has", "flag", "type", "val", "valeur", "value",
    }
)  # fmt: skip

_NUMERIC = frozenset({"integer", "float"})
_TEMPORAL = frozenset({"date", "datetime"})


def type_similarity(a: str, b: str) -> float:
    """Compatibility of two inferred types in [0, 1] (documented table, see module doc)."""
    if a == b:
        return 1.0
    pair = {a, b}
    if pair <= _NUMERIC or pair <= _TEMPORAL:
        return 0.8
    if "empty" in pair:
        return 0.5  # an all-null column carries no type evidence
    if pair == {"boolean", "integer"}:
        return 0.3
    if "string" in pair:
        return 0.2  # values reformatted as text
    return 0.0


class ChangeKind(StrEnum):
    """Kind of change; the value is stored as `schema_change.change_type`."""

    ADDED = "add_column"
    REMOVED = "drop_column"
    RENAMED = "rename_column"
    TYPE_CHANGED = "type_change"
    FORMAT_CHANGED = "format_change"
    NULLABILITY_CHANGED = "nullability_change"
    REORDERED = "reorder"


_KIND_ORDER = {k: i for i, k in enumerate(ChangeKind)}


class DiffConfig(BaseModel):
    """Thresholds (H-02 defaults) and weights of the rename score. Weights are normalised."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    review_threshold: float = Field(default=0.60, gt=0, le=1)
    auto_threshold: float = Field(default=0.90, gt=0, le=1)
    name_weight: float = Field(default=0.7, ge=0)
    type_weight: float = Field(default=0.1, ge=0)
    dist_weight: float = Field(default=0.2, ge=0)
    generic_tokens: frozenset[str] = DEFAULT_GENERIC_TOKENS
    generic_token_weight: float = Field(default=0.25, ge=0, le=1)
    abbreviation_similarity: float = Field(default=0.85, ge=0, le=1)
    token_fuzzy_min: float = Field(default=0.8, gt=0, le=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.auto_threshold < self.review_threshold:
            raise ValueError("auto_threshold must be >= review_threshold")
        if self.name_weight + self.type_weight + self.dist_weight <= 0:
            raise ValueError("the sum of the weights must be > 0")
        return self


class RenameCandidate(BaseModel):
    """Scored (dropped, added) pair, with the score components for the review UI."""

    model_config = ConfigDict(frozen=True)

    old_column: str
    new_column: str
    score: float
    name_sim: float
    type_sim: float
    dist_sim: float
    decision: Decision
    selected: bool = False


class ColumnChange(BaseModel):
    """One change. `column` is the new name (the old one for REMOVED, None for REORDERED)."""

    model_config = ConfigDict(frozen=True)

    kind: ChangeKind
    column: str | None
    old_column: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class SchemaDiff(BaseModel):
    """Changes from an old to a new schema plus every scored rename candidate."""

    model_config = ConfigDict(frozen=True)

    changes: tuple[ColumnChange, ...]
    candidates: tuple[RenameCandidate, ...]

    @property
    def renames(self) -> tuple[RenameCandidate, ...]:
        """Accepted renames (decision auto or review)."""
        return tuple(c for c in self.candidates if c.selected)

    def to_schema_changes(self) -> list[SchemaChange]:
        """Rows for `SchemaRegistry.record_changes` (detail never holds column values)."""
        out = []
        for c in self.changes:
            detail = dict(c.detail)
            if c.old_column is not None and c.kind is not ChangeKind.RENAMED:
                detail["old_column"] = c.old_column
            out.append(SchemaChange(change_type=c.kind.value, column_name=c.column, detail=detail))
        return out


# --- name similarity ---------------------------------------------------------------------------


def _tokens(name: str) -> list[str]:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    return [t for t in re.split(r"[^a-z0-9]+", s.lower()) if t]


def _is_abbrev(short: str, long: str) -> bool:
    if len(short) < 3 or len(short) >= len(long) or short[0] != long[0]:
        return False
    it = iter(long)
    return all(ch in it for ch in short)  # subsequence test


def _token_sim(a: str, b: str, cfg: DiffConfig) -> float:
    if a == b:
        return 1.0
    short, long = sorted((a, b), key=len)
    if _is_abbrev(short, long):
        return cfg.abbreviation_similarity
    r = SequenceMatcher(None, a, b).ratio()
    return r if r >= cfg.token_fuzzy_min else 0.0


def name_similarity(a: str, b: str, cfg: DiffConfig) -> float:
    """Token-alignment similarity of two column names in [0, 1] (see module doc)."""
    ta, tb = _tokens(a), _tokens(b)
    if ta and "".join(ta) == "".join(tb):
        return 1.0
    wa = [cfg.generic_token_weight if t in cfg.generic_tokens else 1.0 for t in ta]
    wb = [cfg.generic_token_weight if t in cfg.generic_tokens else 1.0 for t in tb]
    if sum(wa) == 0 or sum(wb) == 0:
        return 0.0
    # (sim, side-a indices, side-b indices); initials match one token to a run of >= 2 tokens
    pairs: list[tuple[float, tuple[int, ...], tuple[int, ...]]] = []
    for i, x in enumerate(ta):
        for j, y in enumerate(tb):
            if (s := _token_sim(x, y, cfg)) > 0:
                pairs.append((s, (i,), (j,)))
    for src, dst, swap in ((ta, tb, False), (tb, ta, True)):
        for i, x in enumerate(src):
            n = len(x)
            for j in range(len(dst) - n + 1):
                if n >= 2 and "".join(t[0] for t in dst[j : j + n]) == x:
                    run = tuple(range(j, j + n))
                    s = cfg.abbreviation_similarity
                    pairs.append((s, run, (i,)) if swap else (s, (i,), run))
    pairs.sort(key=lambda p: (-p[0], p[1], p[2]))
    used_a: set[int] = set()
    used_b: set[int] = set()
    cov_a = cov_b = 0.0
    for s, ia, ib in pairs:
        if used_a.isdisjoint(ia) and used_b.isdisjoint(ib):
            used_a.update(ia)
            used_b.update(ib)
            cov_a += s * sum(wa[i] for i in ia)
            cov_b += s * sum(wb[j] for j in ib)
    ca, cb = cov_a / sum(wa), cov_b / sum(wb)
    return max(ca, cb) * math.sqrt(min(ca, cb))


# --- distribution similarity -------------------------------------------------------------------


def _close(a: float, b: float) -> float:
    den = abs(a) + abs(b)
    return 1.0 - abs(a - b) / den if den else 1.0


def _iou(lo1: float, hi1: float, lo2: float, hi2: float) -> float:
    union = max(hi1, hi2) - min(lo1, lo2)
    if union == 0:
        return 1.0
    return max(0.0, min(hi1, hi2) - max(lo1, lo2)) / union


def dist_similarity(a: ColumnProfile, b: ColumnProfile) -> float:
    """Closeness of two columns from profile statistics only (see module doc)."""
    parts: list[tuple[float, float]] = []  # (value, weight)
    if a.row_count and b.row_count:
        parts.append((1 - abs(a.null_count / a.row_count - b.null_count / b.row_count), 1.0))
    na, nb = a.row_count - a.null_count, b.row_count - b.null_count
    if na and nb:
        parts.append((1 - abs(a.distinct_count / na - b.distinct_count / nb), 1.0))
    num_a, num_b = a.mean is not None, b.mean is not None
    len_a, len_b = a.min_length is not None, b.min_length is not None
    if num_a and num_b:
        assert a.min is not None and a.max is not None and b.min is not None
        assert b.max is not None and a.mean is not None and b.mean is not None
        iou = _iou(a.min, a.max, b.min, b.max)
        parts.append(((iou + _close(a.mean, b.mean)) / 2, 2.0))
    elif len_a and len_b:
        assert a.max_length is not None and b.min_length is not None
        assert a.min_length is not None and b.max_length is not None
        parts.append((_iou(a.min_length, a.max_length + 1, b.min_length, b.max_length + 1), 2.0))
    elif num_a or num_b or len_a or len_b:
        parts.append((0.0, 2.0))  # incomparable value statistics
    if not parts:
        return 0.0
    return sum(v * w for v, w in parts) / sum(w for _, w in parts)


# --- diff --------------------------------------------------------------------------------------


def _columns(schema: SchemaFingerprint | Sequence[ColumnProfile]) -> list[ColumnProfile]:
    cols = list(schema.columns if isinstance(schema, SchemaFingerprint) else schema)
    names = [c.name for c in cols]
    if len(set(names)) != len(names):
        raise SchemaError("duplicate column name in schema")
    return cols


def _score(a: ColumnProfile, b: ColumnProfile, cfg: DiffConfig) -> RenameCandidate:
    ns = name_similarity(a.name, b.name, cfg)
    ts = type_similarity(a.inferred_type, b.inferred_type)
    ds = dist_similarity(a, b)
    total = cfg.name_weight + cfg.type_weight + cfg.dist_weight
    score = round((cfg.name_weight * ns + cfg.type_weight * ts + cfg.dist_weight * ds) / total, 4)
    decision: Decision = (
        "auto"
        if score >= cfg.auto_threshold
        else "review"
        if score >= cfg.review_threshold
        else "rejected"
    )
    return RenameCandidate(
        old_column=a.name,
        new_column=b.name,
        score=score,
        name_sim=round(ns, 4),
        type_sim=round(ts, 4),
        dist_sim=round(ds, 4),
        decision=decision,
    )


def _attribute_changes(a: ColumnProfile, b: ColumnProfile) -> list[ColumnChange]:
    old = a.name if a.name != b.name else None
    found: list[tuple[ChangeKind, dict[str, Any]]] = []
    if a.inferred_type != b.inferred_type:
        found.append(
            (
                ChangeKind.TYPE_CHANGED,
                {"from": a.inferred_type, "to": b.inferred_type,
                 "from_format": a.format_hint, "to_format": b.format_hint},
            )
        )  # fmt: skip
    elif a.format_hint != b.format_hint:
        found.append(
            (
                ChangeKind.FORMAT_CHANGED,
                {"type": b.inferred_type, "from": a.format_hint, "to": b.format_hint},
            )
        )
    if a.nullable != b.nullable:
        found.append((ChangeKind.NULLABILITY_CHANGED, {"from": a.nullable, "to": b.nullable}))
    return [ColumnChange(kind=k, column=b.name, old_column=old, detail=d) for k, d in found]


def diff_schemas(
    old: SchemaFingerprint | Sequence[ColumnProfile],
    new: SchemaFingerprint | Sequence[ColumnProfile],
    *,
    config: DiffConfig | None = None,
) -> SchemaDiff:
    """Diff two profiled schemas; renames are proposals scored as described in the module doc."""
    cfg = config or DiffConfig()
    a_cols, b_cols = _columns(old), _columns(new)
    a_by, b_by = {c.name: c for c in a_cols}, {c.name: c for c in b_cols}
    dropped = sorted(a_by.keys() - b_by.keys())
    added = sorted(b_by.keys() - a_by.keys())

    scored = sorted(
        (_score(a_by[o], b_by[n], cfg) for o in dropped for n in added),
        key=lambda c: (-c.score, c.old_column, c.new_column),
    )
    used_old: set[str] = set()
    used_new: set[str] = set()
    candidates = []
    for c in scored:
        if (
            c.decision != "rejected"
            and c.old_column not in used_old
            and c.new_column not in used_new
        ):
            used_old.add(c.old_column)
            used_new.add(c.new_column)
            c = c.model_copy(update={"selected": True})
        candidates.append(c)

    pairs = {n: n for n in a_by.keys() & b_by.keys()}  # old name -> new name
    changes: list[ColumnChange] = []
    for c in candidates:
        if c.selected:
            pairs[c.old_column] = c.new_column
            detail = {"from": c.old_column, "to": c.new_column, "score": c.score,
                      "decision": c.decision,
                      "components": {"name_sim": c.name_sim, "type_sim": c.type_sim,
                                     "dist_sim": c.dist_sim}}  # fmt: skip
            if any(
                o is not c and o.decision != "rejected" and o.score == c.score
                and (o.old_column == c.old_column or o.new_column == c.new_column)
                for o in candidates
            ):  # fmt: skip
                detail["tie"] = True
            changes.append(ColumnChange(kind=ChangeKind.RENAMED, column=c.new_column,
                                        old_column=c.old_column, detail=detail))  # fmt: skip
    for n in dropped:
        if n not in used_old:
            col = a_by[n]
            changes.append(ColumnChange(kind=ChangeKind.REMOVED, column=n,
                                        detail={"type": col.inferred_type,
                                                "nullable": col.nullable}))  # fmt: skip
    for n in added:
        if n not in used_new:
            col = b_by[n]
            changes.append(ColumnChange(kind=ChangeKind.ADDED, column=n,
                                        detail={"type": col.inferred_type,
                                                "nullable": col.nullable}))  # fmt: skip
    for o, n in pairs.items():
        changes.extend(_attribute_changes(a_by[o], b_by[n]))

    before = [pairs[c.name] for c in a_cols if c.name in pairs]
    after_names = set(pairs.values())
    after = [c.name for c in b_cols if c.name in after_names]
    if before != after:
        changes.append(ColumnChange(kind=ChangeKind.REORDERED, column=None,
                                    detail={"from": before, "to": after}))  # fmt: skip

    changes.sort(key=lambda c: (_KIND_ORDER[c.kind], c.column or "", c.old_column or ""))
    return SchemaDiff(changes=tuple(changes), candidates=tuple(candidates))
