"""Distribution drift between two batches (P4-02): PSI, two-sample KS, categorical PSI.

Pure and deterministic; results carry aggregate statistics and sizes only, never values.

Conventions:
- PSI: bin edges are quantiles of the REFERENCE (inclusive method); repeated edges are merged
  (degenerate bins disappear), bins are (-inf, c1], (c1, c2], ..., (ck, +inf) so out-of-range
  current values land in the extreme bins. Proportions are floored at `epsilon` (no
  renormalisation, negligible) to avoid log(0).
  PSI is biased upwards by sampling noise: for two samples of the SAME law the expected value is
  about (bins-1)*(1/n + 1/m) (0.09 for bins=10, n=m=200), so the conventional 0.1 threshold is
  only meaningful for n, m of about 500 or more. The default `drifted` rule therefore requires
  strong PSI, or a significant KS together with at least moderate PSI.
- KS: exact statistic in O(n log n) (ties handled by stepping over equal values); the p-value is
  the ASYMPTOTIC Kolmogorov series with Stephens' correction, an approximation, `None` when
  min(n, m) < `KS_MIN_N`.
- `drifted` = psi >= psi_strong OR (ks_pvalue < alpha AND psi >= psi_moderate).
- Fewer than `min_samples` (default 30) usable values on either side -> skipped.
- Categorical: PSI over the union of categories (epsilon floor); new/disappeared categories are
  only COUNTED. `drifted` = psi >= psi_strong (no KS on categories).
"""

import math
from bisect import bisect_left
from collections import Counter
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from adp.errors import QualityError
from adp.quality.anomaly import finite_numbers

PsiLevel = Literal["stable", "moderate", "strong"]
Kind = Literal["numeric", "categorical"]

KS_MIN_N = 8
MAX_ITEMS = 100_000


class DriftConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bins: int = Field(default=10, ge=2, le=100)
    epsilon: float = Field(default=1e-4, gt=0, lt=0.01)
    psi_moderate: float = Field(default=0.1, gt=0)
    psi_strong: float = Field(default=0.25, gt=0)
    alpha: float = Field(default=0.05, gt=0, lt=1)
    min_samples: int = Field(default=30, ge=2)
    max_items: int = Field(default=MAX_ITEMS, ge=1, le=10_000_000)

    @model_validator(mode="after")
    def _ordered(self) -> "DriftConfig":
        if self.psi_moderate >= self.psi_strong:
            raise ValueError("psi_moderate must be < psi_strong")
        return self


class DriftResult(BaseModel):
    """Aggregate verdict; `sizes` = (reference, current) usable counts."""

    model_config = ConfigDict(frozen=True)

    column: str | None = None
    kind: Kind = "numeric"
    sizes: tuple[int, int] = (0, 0)
    psi: float | None = None
    psi_level: PsiLevel | None = None
    ks_stat: float | None = None
    ks_pvalue: float | None = None
    new_categories: int = 0
    disappeared_categories: int = 0
    drifted: bool = False
    reason: str = ""
    skipped_reason: str | None = None

    @property
    def skipped(self) -> bool:
        return self.skipped_reason is not None


def _psi_from_props(ref: Sequence[float], cur: Sequence[float], eps: float) -> float:
    total = 0.0
    for r, c in zip(ref, cur, strict=True):
        r, c = max(r, eps), max(c, eps)
        total += (c - r) * math.log(c / r)
    return total


def psi(
    reference: Sequence[object],
    current: Sequence[object],
    *,
    bins: int = 10,
    epsilon: float = 1e-4,
) -> float:
    """Population Stability Index of `current` against `reference` (>= 0)."""
    ref = sorted(x for _, x in finite_numbers(reference)[0])
    cur = [x for _, x in finite_numbers(current)[0]]
    if not ref or not cur:
        raise QualityError("psi needs at least one numeric value on each side")
    if len(ref) == 1:
        cuts: list[float] = [ref[0]]
    else:
        cuts = sorted(set(_quantiles(ref, bins)))
    k = len(cuts) + 1
    rc, cc = [0] * k, [0] * k
    for x in ref:
        rc[bisect_left(cuts, x)] += 1
    for x in cur:
        cc[bisect_left(cuts, x)] += 1
    return _psi_from_props([n / len(ref) for n in rc], [n / len(cur) for n in cc], epsilon)


def _quantiles(sorted_ref: list[float], bins: int) -> list[float]:
    """bins-1 inclusive-method cut points of an already sorted list (len >= 2)."""
    n = len(sorted_ref)
    out = []
    for i in range(1, bins):
        pos = (n - 1) * i / bins
        lo = int(pos)
        frac = pos - lo
        hi = min(lo + 1, n - 1)
        out.append(sorted_ref[lo] + (sorted_ref[hi] - sorted_ref[lo]) * frac)
    return out


def ks_statistic(reference: Sequence[object], current: Sequence[object]) -> float:
    """Two-sample Kolmogorov-Smirnov distance, exact (ties handled). In [0, 1]."""
    a = sorted(x for _, x in finite_numbers(reference)[0])
    b = sorted(x for _, x in finite_numbers(current)[0])
    if not a or not b:
        raise QualityError("ks needs at least one numeric value on each side")
    n, m = len(a), len(b)
    i = j = 0
    d = 0.0
    while i < n and j < m:
        v = min(a[i], b[j])
        while i < n and a[i] == v:
            i += 1
        while j < m and b[j] == v:
            j += 1
        d = max(d, abs(i / n - j / m))
    return d


def ks_pvalue(d: float, n: int, m: int) -> float | None:
    """Asymptotic KS p-value (Stephens correction); None if min(n, m) < KS_MIN_N. Approximate."""
    if min(n, m) < KS_MIN_N:
        return None
    en = math.sqrt(n * m / (n + m))
    lam = (en + 0.12 + 0.11 / en) * d
    if lam < 0.27:
        return 1.0
    s = 0.0
    for k in range(1, 101):
        term = math.exp(-2.0 * k * k * lam * lam)
        s += term if k % 2 else -term
        if term < 1e-12:
            break
    return min(1.0, max(0.0, 2.0 * s))


def categorical_drift(
    reference: Sequence[object], current: Sequence[object], *, epsilon: float = 1e-4
) -> tuple[float, int, int]:
    """(psi, new categories, disappeared categories); None values ignored."""
    rc = Counter(v for v in reference if v is not None)
    cc = Counter(v for v in current if v is not None)
    nr, nc = sum(rc.values()), sum(cc.values())
    if not nr or not nc:
        raise QualityError("categorical drift needs at least one value on each side")
    cats = sorted(set(rc) | set(cc), key=repr)
    value = _psi_from_props([rc[c] / nr for c in cats], [cc[c] / nc for c in cats], epsilon)
    return value, len(set(cc) - set(rc)), len(set(rc) - set(cc))


def _level(value: float, cfg: DriftConfig) -> PsiLevel:
    if value >= cfg.psi_strong:
        return "strong"
    return "moderate" if value >= cfg.psi_moderate else "stable"


def detect_drift(
    reference: Sequence[object],
    current: Sequence[object],
    *,
    config: DriftConfig | None = None,
    column: str | None = None,
    kind: Kind = "numeric",
) -> DriftResult:
    """Compare `current` to `reference`; skipped when either side has < min_samples values."""
    cfg = config or DriftConfig()
    if max(len(reference), len(current)) > cfg.max_items:
        raise QualityError(f"too many values for drift detection (max {cfg.max_items})")
    if kind == "categorical":
        r = [v for v in reference if v is not None]
        c = [v for v in current if v is not None]
        sizes = (len(r), len(c))
        if min(sizes) < cfg.min_samples:
            return DriftResult(column=column, kind=kind, sizes=sizes,
                               skipped_reason="too_few_samples")  # fmt: skip
        p, new, gone = categorical_drift(r, c, epsilon=cfg.epsilon)
        level = _level(p, cfg)
        return DriftResult(column=column, kind=kind, sizes=sizes, psi=p, psi_level=level,
                           new_categories=new, disappeared_categories=gone,
                           drifted=level == "strong", reason=f"psi_{level}")  # fmt: skip
    r = [x for _, x in finite_numbers(reference)[0]]
    c = [x for _, x in finite_numbers(current)[0]]
    sizes = (len(r), len(c))
    if min(sizes) < cfg.min_samples:
        return DriftResult(column=column, sizes=sizes, skipped_reason="too_few_samples")
    p = psi(r, c, bins=cfg.bins, epsilon=cfg.epsilon)
    d = ks_statistic(r, c)
    pv = ks_pvalue(d, *sizes)
    level = _level(p, cfg)
    significant = pv is not None and pv < cfg.alpha
    if level == "strong":
        drifted, reason = True, "psi_strong"
    elif significant and level == "moderate":
        drifted, reason = True, "ks_significant_and_psi_moderate"
    else:
        drifted, reason = False, f"psi_{level}" + ("_ks_significant" if significant else "")
    return DriftResult(column=column, sizes=sizes, psi=p, psi_level=level, ks_stat=d,
                       ks_pvalue=pv, drifted=drifted, reason=reason)  # fmt: skip
