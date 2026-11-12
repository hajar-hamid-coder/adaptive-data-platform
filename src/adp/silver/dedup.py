"""Silver deduplication (P3-07): exact natural-key duplicates, then blocked fuzzy duplicates.

Key semantics (`key_fields`):
- text value (str): compared through `normalize` (accents, case, separators, spaces folded);
- any other value (year, number, date): a HARD field, compared by strict equality. Two rows with
  different hard values are never duplicates (same region, different years = a time series);
- null (or text that normalizes to nothing): the row is NOT grouped with any other row. Two rows
  with a null key are not evidence of a duplicate (decision, tested); they always survive.

Fuzzy pass: rows are bucketed by (hard values, text field, prefix/suffix of the normalized text)
so only rows sharing a bucket are scored (no O(n^2) over the table). The pair score is the MIN over
text key fields of max(ratio, token_sort_ratio) (rapidfuzz); texts whose digits differ score 0
(`IND_0001` vs `IND_0002` are different). Thresholds follow H-02: score >= auto -> merged, in
[review, auto) -> listed in the report, NEVER merged, below -> distinct. Groups are connected
components of the "auto" pairs (union-find; chaining A~B~C merges A and C by design).

Survivor: fewest nulls, then latest `sort_field` (optional), then first in input order; with
`coalesce=True` its nulls are filled from the other members. Pure and deterministic; the report
holds ROW INDICES, scores and counts only, never a value.
"""

import json
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict
from rapidfuzz import fuzz

from adp.errors import ConfigError, SchemaError
from adp.logging import get_logger
from adp.mapping.dictionary import normalize
from adp.mapping.llm import AUTO_THRESHOLD, REVIEW_THRESHOLD
from adp.mapping.transform import Value

log = get_logger(__name__)

_DIGITS = re.compile(r"\d+")


@dataclass(frozen=True)
class DedupConfig:
    auto_threshold: float = AUTO_THRESHOLD
    review_threshold: float = REVIEW_THRESHOLD
    block_prefix: int = 2  # characters of prefix/suffix used as blocking keys
    sort_field: str | None = None  # "most recent" = greatest value of this column
    coalesce: bool = False  # fill the survivor's nulls from the other members

    def __post_init__(self) -> None:
        if not 0.0 <= self.review_threshold <= self.auto_threshold <= 1.0:
            raise ConfigError("dedup thresholds must satisfy 0 <= review <= auto <= 1")
        if self.block_prefix < 1:
            raise ConfigError("dedup block_prefix must be >= 1")


class DuplicateGroup(BaseModel):
    model_config = ConfigDict(frozen=True)
    indices: tuple[int, ...]  # input row indices, ascending
    survivor: int
    score: float  # weakest link inside the group (1.0 for exact)
    method: Literal["exact", "fuzzy"]


class ReviewPair(BaseModel):
    model_config = ConfigDict(frozen=True)
    a: int
    b: int
    score: float


class DedupReport(BaseModel):
    model_config = ConfigDict(frozen=True)
    rows_read: int
    survivors: int
    eliminated: int
    groups_exact: int
    groups_fuzzy: int
    comparisons: int  # fuzzy pairs scored (shows blocking works)
    groups: tuple[DuplicateGroup, ...]
    review_pairs: tuple[ReviewPair, ...]

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class DedupResult:
    survivors: list[list[Value]]
    survivor_indices: list[int]  # original row index of each survivor
    groups: list[DuplicateGroup]
    report: DedupReport


def render_report(report: DedupReport) -> str:
    """French summary: counts and row indices only."""
    lines = [
        f"Déduplication : {report.rows_read} lignes lues, {report.survivors} conservées, "
        f"{report.eliminated} éliminées.",
        f"Groupes : {report.groups_exact} exacts, {report.groups_fuzzy} approximatifs ; "
        f"{len(report.review_pairs)} paires à revoir (non fusionnées) ; "
        f"{report.comparisons} comparaisons approximatives.",
    ]
    lines += [
        f"- groupe {g.method} (score {g.score:.2f}) : lignes {list(g.indices)}, "
        f"survivante {g.survivor}"
        for g in report.groups
    ]
    lines += [
        f"- à revoir : lignes {p.a} et {p.b} (score {p.score:.2f})" for p in report.review_pairs
    ]
    return "\n".join(lines)


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:  # the smallest index stays root: deterministic
            self.parent[max(ra, rb)] = min(ra, rb)


def _field_score(a: str, b: str, cutoff: float) -> float:
    if _DIGITS.findall(a) != _DIGITS.findall(b):
        return 0.0
    c = cutoff * 100
    return max(fuzz.ratio(a, b, score_cutoff=c), fuzz.token_sort_ratio(a, b, score_cutoff=c)) / 100


def _nulls(row: Sequence[Value]) -> int:
    return sum(v is None for v in row)


def _pick_survivor(
    members: list[int], rows: Sequence[Sequence[Value]], sort_idx: int | None
) -> int:
    best = members[0]
    for i in members[1:]:
        n_i, n_b = _nulls(rows[i]), _nulls(rows[best])
        if n_i != n_b:
            if n_i < n_b:
                best = i
            continue
        if sort_idx is None:
            continue
        vi, vb = rows[i][sort_idx], rows[best][sort_idx]
        if vi is None:
            continue
        try:
            newer = vb is None or vi > vb  # type: ignore[operator]
        except TypeError as exc:
            raise SchemaError("sort_field holds values that cannot be ordered together") from exc
        if newer:
            best = i
    return best


def dedupe(
    columns: Sequence[str],
    rows: Sequence[Sequence[Value]],
    *,
    key_fields: Sequence[str],
    config: DedupConfig = DedupConfig(),  # noqa: B008 (frozen)
) -> DedupResult:
    """Group exact and fuzzy duplicates on `key_fields`, keep one survivor per group."""
    if not key_fields or len(set(key_fields)) != len(key_fields):
        raise ConfigError("key_fields must be a non-empty list without duplicates")
    pos = {c: i for i, c in enumerate(columns)}
    missing = [f for f in key_fields if f not in pos]
    if missing or (config.sort_field is not None and config.sort_field not in pos):
        absent = missing + ([config.sort_field] if config.sort_field not in pos else [])
        raise SchemaError(f"columns absent from the table: {', '.join(map(str, absent))}")
    if any(len(r) != len(columns) for r in rows):
        raise SchemaError("row length differs from the number of columns")
    kidx = [pos[f] for f in key_fields]
    sort_idx = pos[config.sort_field] if config.sort_field is not None else None

    # Per-row key: hard values, normalized texts (None = row never grouped).
    hard: dict[int, tuple[tuple[str, Value], ...]] = {}
    texts: dict[int, tuple[tuple[str, str], ...]] = {}
    exact_buckets: dict[tuple[object, ...], list[int]] = defaultdict(list)
    for i, row in enumerate(rows):
        h: list[tuple[str, Value]] = []
        t: list[tuple[str, str]] = []
        for name, j in zip(key_fields, kidx, strict=True):
            v = row[j]
            if isinstance(v, str):
                n = normalize(v)
                if not n:
                    break
                t.append((name, n.replace("_", " ")))
            elif v is None:
                break
            else:
                h.append((name, v))
        else:
            hard[i], texts[i] = tuple(h), tuple(t)
            exact_buckets[(tuple(h), tuple(t))].append(i)

    uf = _UnionFind(len(rows))
    edges: list[tuple[int, int, float]] = []
    for members in exact_buckets.values():
        for k in members[1:]:
            uf.union(members[0], k)
            edges.append((members[0], k, 1.0))

    # Fuzzy pass over blocked candidate pairs.
    blocks: dict[tuple[object, ...], list[int]] = defaultdict(list)
    bp = config.block_prefix
    for i, row_texts in texts.items():
        sig = (hard[i], tuple(name for name, _ in row_texts))
        for name, txt in row_texts:
            compact = txt.replace(" ", "")
            blocks[(sig, name, "p", compact[:bp])].append(i)
            blocks[(sig, name, "s", compact[-bp:])].append(i)
    pairs = {
        (a, b) for m in blocks.values() if len(m) > 1 for x, a in enumerate(m) for b in m[x + 1 :]
    }
    scored: dict[tuple[int, int], float] = {}
    for a, b in sorted(pairs):
        if uf.find(a) == uf.find(b):
            continue
        score = min(
            _field_score(x[1], y[1], config.review_threshold)
            for x, y in zip(texts[a], texts[b], strict=True)
        )
        scored[(a, b)] = score
        if score >= config.auto_threshold:
            uf.union(a, b)
            edges.append((a, b, score))

    comps: dict[int, list[int]] = defaultdict(list)
    for i in range(len(rows)):
        comps[uf.find(i)].append(i)
    weakest: dict[int, float] = {}
    for a, _, edge_score in edges:
        r = uf.find(a)
        weakest[r] = min(weakest.get(r, 1.0), edge_score)

    groups: list[DuplicateGroup] = []
    out: dict[int, list[Value]] = {}
    for root in sorted(comps):
        members = comps[root]
        if len(members) == 1:
            out[members[0]] = list(rows[members[0]])
            continue
        surv = _pick_survivor(members, rows, sort_idx)
        row = list(rows[surv])
        if config.coalesce:
            for others in (m for m in members if m != surv):
                row = [v if v is not None else rows[others][j] for j, v in enumerate(row)]
        out[surv] = row
        exact = len({(hard[m], texts[m]) for m in members}) == 1
        groups.append(
            DuplicateGroup(
                indices=tuple(members),
                survivor=surv,
                score=round(weakest.get(root, 1.0), 4),
                method="exact" if exact else "fuzzy",
            )
        )
    review = tuple(
        ReviewPair(a=a, b=b, score=round(s, 4))
        for (a, b), s in sorted(scored.items())
        if config.review_threshold <= s < config.auto_threshold and uf.find(a) != uf.find(b)
    )
    kept = sorted(out)
    report = DedupReport(
        rows_read=len(rows),
        survivors=len(kept),
        eliminated=len(rows) - len(kept),
        groups_exact=sum(g.method == "exact" for g in groups),
        groups_fuzzy=sum(g.method == "fuzzy" for g in groups),
        comparisons=len(scored),
        groups=tuple(sorted(groups, key=lambda g: g.indices)),
        review_pairs=review,
    )
    log.info(
        "dedup_done",
        rows_read=report.rows_read,
        survivors=report.survivors,
        groups_exact=report.groups_exact,
        groups_fuzzy=report.groups_fuzzy,
        review_pairs=len(review),
        comparisons=report.comparisons,
    )
    return DedupResult([out[i] for i in kept], kept, list(report.groups), report)
