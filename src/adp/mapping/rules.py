"""Deterministic column -> canonical field mapping (rules cascade + fuzzy), 100 % local.

Cascade, best method wins per target field (one candidate per field):
1. exact:      raw column name == canonical field name (score `exact_score`, 1.0).
2. normalized: normalize(column) == normalize(field name) (`normalized_score`, 0.98).
3. synonym:    normalize(column) is a normalized synonym of the field (`synonym_score`, 0.95).
4. fuzzy:      rapidfuzz on the normalized forms (tokens joined by spaces) against every term
   (name + synonyms) of every field; best term kept per field. Similarity = max of
   `fuzz.ratio` (typos, truncations: unempl_rt ~ unemployment_rate) and `fuzz.token_sort_ratio`
   (word order). `token_set_ratio`/`partial_ratio` are NOT used: they return 100 as soon as one
   string is a subset of the other ("taux" vs "taux_chomage"), a false-positive factory.
   Fuzzy score = similarity/100 * `fuzzy_weight` (0.94), so fuzzy never reaches the rules scores.
   Guard: a column made only of generic tokens (id, date, taux, total, nb...) gets no fuzzy
   candidate. Candidates below `min_score` (0.60, H-02 rejection threshold) are dropped.

Auto/review decisions (H-02: >= 0.90 auto, 0.60-0.90 review) are NOT taken here (P3-04).
Ties: sorted by score desc then target name; the leading candidate is flagged `tie` when
another candidate has the same score. Names only; no data values are ever read.
"""

from collections.abc import Iterable, Mapping
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from rapidfuzz import fuzz

from adp.mapping.dictionary import CanonicalField, SemanticDictionary, normalize
from adp.schema.profile import ColumnProfile, InferredType

Method = Literal["exact", "normalized", "synonym", "fuzzy"]

GENERIC_TOKENS: frozenset[str] = frozenset(
    {"id", "code", "num", "no", "nb", "nbr", "nombre", "date", "dt", "taux", "rate", "pct",
     "total", "totale", "type", "val", "valeur", "value", "name", "nom"}
)  # fmt: skip

_NUMERIC: frozenset[str] = frozenset({"integer", "float"})
_TYPE_OK: dict[str, frozenset[str]] = {
    "integer": frozenset({"integer"}),
    "float": _NUMERIC,
    "boolean": frozenset({"boolean"}),
    "date": frozenset({"date", "datetime", "integer"}),  # a bare year/period may be an int
    "datetime": frozenset({"date", "datetime"}),
    "string": frozenset({"string", "integer", "boolean", "date", "datetime", "float"}),
}


class RulesConfig(BaseModel):
    """Scores per method and thresholds (all in [0, 1])."""

    model_config = ConfigDict(frozen=True)

    exact_score: float = Field(default=1.0, ge=0, le=1)
    normalized_score: float = Field(default=0.98, ge=0, le=1)
    synonym_score: float = Field(default=0.95, ge=0, le=1)
    fuzzy_weight: float = Field(default=0.94, gt=0, le=1)
    min_score: float = Field(default=0.60, ge=0, le=1)
    top_k: int = Field(default=3, ge=1)
    generic_tokens: frozenset[str] = GENERIC_TOKENS

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if not self.exact_score >= self.normalized_score >= self.synonym_score >= self.fuzzy_weight:
            raise ValueError("scores must satisfy exact >= normalized >= synonym >= fuzzy_weight")
        return self


class MappingCandidate(BaseModel):
    """One proposed target for a source column (a proposal, never applied here)."""

    model_config = ConfigDict(frozen=True)

    source_col: str
    target: str
    score: float = Field(ge=0, le=1)
    method: Method
    detail: str = ""  # matched term, e.g. "unemployment_rate"; no data value
    tie: bool = False
    type_compatible: bool | None = None  # None when no profile was given


def _similarity(a: str, b: str) -> float:
    sa, sb = a.replace("_", " "), b.replace("_", " ")
    return max(fuzz.ratio(sa, sb), fuzz.token_sort_ratio(sa, sb)) / 100


def _type_compatible(field: CanonicalField, profile: ColumnProfile | None) -> bool | None:
    if profile is None or profile.inferred_type == "empty":
        return None
    inferred: InferredType = profile.inferred_type
    return inferred in _TYPE_OK[field.type]


def _best(
    col: str, norm: str, field: CanonicalField, cfg: RulesConfig, fuzzy_ok: bool
) -> tuple[float, Method, str] | None:
    """Best (score, method, term) for one field, or None."""
    if col == field.name:
        return cfg.exact_score, "exact", field.name
    if norm == normalize(field.name):
        return cfg.normalized_score, "normalized", field.name
    for s in field.synonyms:
        if norm == normalize(s):
            return cfg.synonym_score, "synonym", normalize(s)
    if not fuzzy_ok:
        return None
    terms = sorted(field.terms())
    sim, term = max(((_similarity(norm, t), t) for t in terms), key=lambda p: (p[0], p[1]))
    return sim * cfg.fuzzy_weight, "fuzzy", term


def map_column(
    source_col: str | None,
    dictionary: SemanticDictionary,
    *,
    config: RulesConfig = RulesConfig(),  # noqa: B008 - frozen, immutable default
    profile: ColumnProfile | None = None,
) -> list[MappingCandidate]:
    """Ranked candidates (<= top_k, score >= min_score) for one column name.

    `profile` only sets `type_compatible` (inferred type vs canonical type, no values used);
    it never changes the score (the decision belongs to P3-04).
    """
    col = (source_col or "").strip()
    norm = normalize(col)
    if not norm:
        return []
    tokens = set(norm.split("_"))
    fuzzy_ok = not tokens <= config.generic_tokens
    found: list[MappingCandidate] = []
    for f in dictionary.fields:
        best = _best(col, norm, f, config, fuzzy_ok)
        if best is None or best[0] < config.min_score:
            continue
        score, method, term = best
        found.append(
            MappingCandidate(
                source_col=source_col or "",
                target=f.name,
                score=round(score, 4),
                method=method,
                detail=term,
                type_compatible=_type_compatible(f, profile),
            )
        )
    found.sort(key=lambda c: (-c.score, c.target))
    top = found[: config.top_k]
    if len(top) > 1 and top[0].score == top[1].score:
        top[0] = top[0].model_copy(update={"tie": True})
    return top


def map_columns(
    columns: Iterable[str],
    dictionary: SemanticDictionary,
    config: RulesConfig = RulesConfig(),  # noqa: B008
    profiles: Mapping[str, ColumnProfile] | None = None,
) -> dict[str, list[MappingCandidate]]:
    """Map every column (input order preserved)."""
    return {
        c: map_column(c, dictionary, config=config, profile=(profiles or {}).get(c))
        for c in columns
    }
