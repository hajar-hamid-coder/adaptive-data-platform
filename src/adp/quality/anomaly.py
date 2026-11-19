"""Outlier detection on one numeric column (P4-02): z-score, robust z-score (median/MAD), IQR.

Pure and deterministic. Results hold counts, row INDICES and (optionally) aggregate bounds,
never data values. SENSITIVITY: `bounds` are statistics derived from the batch (mean +- k*sd,
quartile fences...) and can reveal the order of magnitude of the data; call with
`include_bounds=False` for any persisted report.

Conventions:
- `None` is ignored (original row indices are kept); non-numbers, `bool`, NaN and +-inf are
  ignored and counted in `ignored_non_numeric`.
- Fewer than `min_samples` usable values -> `skipped_reason="too_few_samples"` (no false alarm).
  Note: with n values a classical z-score cannot exceed (n-1)/sqrt(n), so z=3 needs n >= 11.
- zscore: population standard deviation; sd == 0 (all equal) -> no outlier.
- robust_zscore: modified z = 0.6745*(x-median)/MAD. When MAD == 0 (more than half of the values
  identical) it falls back to the mean absolute deviation (0.7979*(x-median)/meanAD); if that is
  0 too (all equal) there is no outlier.
- iqr: Tukey fences [Q1 - k*IQR, Q3 + k*IQR], quartiles by the inclusive method. IQR == 0 makes
  the fences collapse onto Q1/Q3: any value beyond them is flagged (standard Tukey behaviour).
- `max_items` bounds the input size (QualityError beyond), against resource abuse.
"""

import math
import statistics
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from adp.errors import ConfigError, QualityError
from adp.mapping.dictionary import SemanticDictionary
from adp.mapping.transform import Value

Method = Literal["zscore", "robust_zscore", "iqr"]

DEFAULT_THRESHOLDS: dict[str, float] = {"zscore": 3.0, "robust_zscore": 3.5, "iqr": 1.5}
MAX_ITEMS = 100_000
_MAD_SCALE = 0.6745
_MEANAD_SCALE = 0.7979


class AnomalyConfig(BaseModel):
    """Thresholds for one detection; `threshold=None` uses the method default."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    threshold: float | None = Field(default=None, gt=0, le=1000)
    min_samples: int = Field(default=8, ge=3, le=MAX_ITEMS)
    max_items: int = Field(default=MAX_ITEMS, ge=1, le=10_000_000)


class ColumnAnomaly(AnomalyConfig):
    """Per-column configuration of `detect_in_table`."""

    method: Method


class Bounds(BaseModel):
    model_config = ConfigDict(frozen=True)
    low: float
    high: float


class AnomalyResult(BaseModel):
    """Outcome on one column; indices refer to the ORIGINAL positions in the input."""

    model_config = ConfigDict(frozen=True)

    method: Method
    evaluated: int
    ignored_none: int = 0
    ignored_non_numeric: int = 0
    outlier_indices: tuple[int, ...] = ()
    outlier_count: int = 0
    bounds: Bounds | None = None
    skipped_reason: str | None = None

    @property
    def skipped(self) -> bool:
        return self.skipped_reason is not None


def finite_numbers(values: Sequence[object]) -> tuple[list[tuple[int, float]], int, int]:
    """(index, float) of usable numbers, count of None, count of other ignored values."""
    kept: list[tuple[int, float]] = []
    nones = other = 0
    for i, v in enumerate(values):
        if v is None:
            nones += 1
        elif isinstance(v, bool) or not isinstance(v, int | float):
            other += 1
        else:
            try:
                f = float(v)
            except OverflowError:
                other += 1
                continue
            if math.isfinite(f):
                kept.append((i, f))
            else:
                other += 1
    return kept, nones, other


def _bounds(xs: list[float], method: str, t: float) -> tuple[float, float]:
    if method == "zscore":
        mean, sd = statistics.fmean(xs), statistics.pstdev(xs)
        return mean - t * sd, mean + t * sd
    if method == "robust_zscore":
        med = statistics.median(xs)
        mad = statistics.median(abs(x - med) for x in xs)
        if mad > 0:
            scale = mad / _MAD_SCALE
        else:  # fallback documented in the module docstring
            scale = statistics.fmean(abs(x - med) for x in xs) / _MEANAD_SCALE
        return med - t * scale, med + t * scale
    q1, _, q3 = statistics.quantiles(xs, n=4, method="inclusive")
    return q1 - t * (q3 - q1), q3 + t * (q3 - q1)


def detect_outliers(
    values: Sequence[object],
    *,
    method: Method,
    config: AnomalyConfig | None = None,
    include_bounds: bool = True,
) -> AnomalyResult:
    """Flag outliers of `values` (row order preserved). Never raises on odd values."""
    cfg = config or AnomalyConfig()
    if len(values) > cfg.max_items:
        raise QualityError(f"too many values for anomaly detection (max {cfg.max_items})")
    pairs, nones, other = finite_numbers(values)

    def skipped(reason: str) -> AnomalyResult:
        return AnomalyResult(method=method, evaluated=len(pairs), ignored_none=nones,
                             ignored_non_numeric=other, skipped_reason=reason)  # fmt: skip

    if len(pairs) < cfg.min_samples:
        return skipped("too_few_samples")
    t = cfg.threshold if cfg.threshold is not None else DEFAULT_THRESHOLDS[method]
    try:
        low, high = _bounds([x for _, x in pairs], method, t)
    except (OverflowError, ValueError):
        low = high = math.nan
    if not (math.isfinite(low) and math.isfinite(high)):
        return skipped("numeric_overflow")
    out = tuple(i for i, x in pairs if x < low or x > high)
    return AnomalyResult(
        method=method,
        evaluated=len(pairs),
        ignored_none=nones,
        ignored_non_numeric=other,
        outlier_indices=out,
        outlier_count=len(out),
        bounds=Bounds(low=low, high=high) if include_bounds else None,
    )


def detect_in_table(
    columns: Sequence[str],
    rows: Sequence[Sequence[Value]],
    column_configs: Mapping[str, ColumnAnomaly],
    *,
    include_bounds: bool = True,
) -> dict[str, AnomalyResult]:
    """Apply each configured column's detection; an absent column is `skipped` (column_absent)."""
    index = {c: i for i, c in reversed(list(enumerate(columns)))}
    results: dict[str, AnomalyResult] = {}
    for name, cc in column_configs.items():
        if name not in index:
            results[name] = AnomalyResult(method=cc.method, evaluated=0,
                                          skipped_reason="column_absent")  # fmt: skip
            continue
        j = index[name]
        results[name] = detect_outliers(
            [r[j] for r in rows], method=cc.method, config=cc, include_bounds=include_bounds
        )
    return results


# --- configuration ---------------------------------------------------------------------------


class _AnomalyFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    columns: dict[str, ColumnAnomaly] = Field(min_length=1, max_length=200)


def load_anomaly_config(
    path: Path, dictionary: SemanticDictionary | None = None
) -> dict[str, ColumnAnomaly]:
    """Load `config/quality/anomaly.yaml` (strict); errors name the file and field."""
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: cannot read YAML ({type(exc).__name__})") from exc
    try:
        parsed = _AnomalyFile.model_validate(raw)
    except ValidationError as exc:
        msg = "; ".join(
            f"field '{'.'.join(str(p) for p in e['loc']) or '?'}': {e['msg']}" for e in exc.errors()
        )
        raise ConfigError(f"{path}: invalid anomaly config: {msg}") from exc
    if dictionary is not None:
        known = {f.name for f in dictionary.fields}
        for col in parsed.columns:
            if col not in known:
                raise ConfigError(f"{path}: column '{col[:64]}' not in the glossary")
    return parsed.columns
