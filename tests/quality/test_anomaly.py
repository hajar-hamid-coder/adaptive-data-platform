"""Outlier detection (P4-02): z-score, robust z-score, IQR, table adapter and YAML config."""

import math
import random
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from adp.connectors.files import read_file
from adp.errors import ConfigError, QualityError
from adp.mapping.dictionary import load_dictionary
from adp.mapping.rules import map_columns
from adp.mapping.transform import load_field_transforms
from adp.quality.anomaly import (
    AnomalyConfig,
    ColumnAnomaly,
    detect_in_table,
    detect_outliers,
    load_anomaly_config,
)
from adp.silver.clean import clean_table, load_cleaning

ROOT = Path(__file__).resolve().parents[2]
SEM = ROOT / "config" / "semantic"
DICT = load_dictionary(SEM / "canonical.yaml")
TRANSFORMS = load_field_transforms(SEM / "transforms.yaml", dictionary=DICT)
RULES = load_cleaning(SEM / "cleaning.yaml", DICT)
T0 = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)
METHODS = ("zscore", "robust_zscore", "iqr")


def sample(n: int = 60, seed: int = 7) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(100, 5) for _ in range(n)]


@pytest.mark.parametrize("method", METHODS)
def test_injected_outlier_detected_at_exact_index(method: str) -> None:
    xs = sample()
    xs[17] = 400.0
    res = detect_outliers(xs, method=method)  # type: ignore[arg-type]
    assert 17 in res.outlier_indices
    assert res.outlier_count == len(res.outlier_indices)
    assert res.bounds is not None and res.bounds.high < 400.0
    if method != "iqr":  # Tukey 1.5 may flag a few natural tails; others stay exact
        assert res.outlier_indices == (17,)


@pytest.mark.parametrize("method", METHODS)
def test_low_outlier_and_none_keep_original_indices(method: str) -> None:
    xs: list[object] = list(sample())
    xs[3] = None
    xs[9] = -300.0
    xs[10] = "abc"
    res = detect_outliers(xs, method=method)  # type: ignore[arg-type]
    assert 9 in res.outlier_indices and 3 not in res.outlier_indices
    assert res.ignored_none == 1 and res.ignored_non_numeric == 1
    assert res.evaluated == len(xs) - 2


def test_clean_sample_no_extreme_alert_for_robust_methods() -> None:
    assert detect_outliers(sample(), method="zscore").outlier_count == 0


def test_include_bounds_false_hides_bounds() -> None:
    xs = sample()
    res = detect_outliers(xs, method="iqr", include_bounds=False)
    assert res.bounds is None
    dump = res.model_dump_json()
    assert not any(f"{x}" in dump for x in xs[:5])


def test_threshold_is_configurable() -> None:
    xs = sample()
    xs[0] = 118.0  # about 3.6 sd
    loose = detect_outliers(xs, method="zscore", config=AnomalyConfig(threshold=6.0))
    tight = detect_outliers(xs, method="zscore", config=AnomalyConfig(threshold=2.0))
    assert loose.outlier_count < tight.outlier_count and 0 in tight.outlier_indices


# --- edge cases -------------------------------------------------------------------------------


@pytest.mark.parametrize("method", METHODS)
def test_edge_cases(method: str) -> None:
    m: dict = {"method": method}
    assert detect_outliers([], **m).skipped_reason == "too_few_samples"
    assert detect_outliers([None] * 20, **m).skipped_reason == "too_few_samples"
    assert detect_outliers([5.0], **m).skipped_reason == "too_few_samples"
    assert detect_outliers(sample(7), **m).skipped_reason == "too_few_samples"
    const = detect_outliers([3.0] * 30, **m)
    assert const.outlier_count == 0 and not const.skipped
    junk: list[object] = [math.nan, math.inf, -math.inf, True, "x", 10**400, *sample(20)]
    res = detect_outliers(junk, **m)
    assert res.ignored_non_numeric == 6 and res.evaluated == 20 and res.outlier_count <= 20


def test_mad_zero_fallback_flags_the_deviant_value() -> None:
    xs = [10.0] * 30 + [10.5, 500.0]
    res = detect_outliers(xs, method="robust_zscore")
    assert res.outlier_indices[-1] == 31 and not res.skipped


def test_too_many_items_refused() -> None:
    with pytest.raises(QualityError):
        detect_outliers([1.0] * 11, method="iqr", config=AnomalyConfig(max_items=10))


def test_huge_values_do_not_raise() -> None:
    res = detect_outliers([1e308, -1e308] * 10, method="zscore")
    assert res.skipped_reason in (None, "numeric_overflow")


# --- tables -----------------------------------------------------------------------------------


def cleaned_v1():
    table = read_file(ROOT / "tests" / "data" / "drift" / "v1" / "population.csv")
    cands = map_columns(table.columns, DICT)
    mapping = {c: m[0].target for c, m in cands.items() if m}
    return clean_table(table, mapping, dictionary=DICT, transforms=TRANSFORMS, text_rules=RULES,
                       source="v1", run_id="r", now=lambda: T0)  # fmt: skip


def test_table_outliers_at_right_rows() -> None:
    res = cleaned_v1()
    rows = [list(r) for r in res.rows]
    ip, iu = res.columns.index("population_total"), res.columns.index("unemployment_rate")
    rows[5][ip] = rows[5][ip] * 100  # type: ignore[operator]
    rows[30][iu] = -40.0
    cfg = {
        "population_total": ColumnAnomaly(method="iqr", threshold=3.0),
        "unemployment_rate": ColumnAnomaly(method="robust_zscore"),
        "activity_rate": ColumnAnomaly(method="iqr"),
    }
    out = detect_in_table(res.columns, rows, cfg)
    assert 5 in out["population_total"].outlier_indices
    assert out["unemployment_rate"].outlier_indices == (30,)
    assert out["activity_rate"].skipped_reason == "column_absent"
    clean = detect_in_table(res.columns, res.rows, {"unemployment_rate": cfg["unemployment_rate"]})
    assert clean["unemployment_rate"].outlier_count == 0


# --- config -----------------------------------------------------------------------------------


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "a.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_shipped_config_is_valid() -> None:
    cfg = load_anomaly_config(ROOT / "config" / "quality" / "anomaly.yaml", DICT)
    assert (
        cfg["unemployment_rate"].method == "robust_zscore"
        and cfg["population_total"].threshold == 3.0
    )


@pytest.mark.parametrize(
    "text",
    [
        "columns: {population_total: {method: iqr, bogus: 1}}",
        "columns: {population_total: {method: mad}}",
        "columns: {population_total: {method: iqr, threshold: -1}}",
        "columns: {population_total: {method: iqr, min_samples: 1}}",
        "columns: {not_a_field: {method: iqr}}",
        "columns: {}",
        "extra: 1\ncolumns: {population_total: {method: iqr}}",
        "columns: !!python/object/apply:os.getcwd []",
        "columns: [",
    ],
)
def test_invalid_config_rejected(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError):
        load_anomaly_config(write(tmp_path, text), DICT)


# --- properties -------------------------------------------------------------------------------

anything = st.lists(
    st.one_of(st.none(), st.booleans(), st.integers(), st.floats(), st.text(max_size=3)),
    max_size=60,
)


@settings(derandomize=True, database=None, max_examples=150, deadline=None)
@given(xs=anything, method=st.sampled_from(METHODS))
def test_never_raises_and_indices_valid(xs: list[object], method: str) -> None:
    res = detect_outliers(xs, method=method)  # type: ignore[arg-type]
    assert all(0 <= i < len(xs) for i in res.outlier_indices)
    assert res.outlier_count == len(res.outlier_indices) <= res.evaluated
    assert list(res.outlier_indices) == sorted(set(res.outlier_indices))
