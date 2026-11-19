"""Distribution drift (P4-02): PSI, KS, categorical, fixtures and properties."""

import importlib.util
import math
import random
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from adp.connectors.files import read_file
from adp.errors import QualityError
from adp.mapping.dictionary import load_dictionary
from adp.mapping.rules import map_columns
from adp.mapping.transform import load_field_transforms
from adp.quality.drift import (
    DriftConfig,
    categorical_drift,
    detect_drift,
    ks_pvalue,
    ks_statistic,
    psi,
)
from adp.silver.clean import clean_table, load_cleaning

ROOT = Path(__file__).resolve().parents[2]
SEM = ROOT / "config" / "semantic"
DICT = load_dictionary(SEM / "canonical.yaml")
TRANSFORMS = load_field_transforms(SEM / "transforms.yaml", dictionary=DICT)
RULES = load_cleaning(SEM / "cleaning.yaml", DICT)
T0 = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)


def gauss(n: int, seed: int, mu: float = 0.0) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(mu, 1.0) for _ in range(n)]


# --- hand-computed references ----------------------------------------------------------------


def test_ks_hand_values() -> None:
    assert ks_statistic([1, 2, 3, 4], [1, 2, 3, 4]) == 0.0
    assert ks_statistic([1, 2, 3], [10, 11]) == 1.0
    # F_a: .25 .5 .75 1 at 1..4 ; F_b: 0 0 .5 1 at 1..4 (b = [3, 4]) -> max gap .5 at 1..2
    assert ks_statistic([1, 2, 3, 4], [3, 4]) == pytest.approx(0.5, abs=1e-9)
    # ties: a=[1,1,2], b=[1,2,2] : at 1 -> |2/3-1/3|=1/3 ; at 2 -> 0
    assert ks_statistic([1, 1, 2], [1, 2, 2]) == pytest.approx(1 / 3, abs=1e-9)


def test_psi_hand_value() -> None:
    # ref [1,2,3,4] bins=2 -> cut 2.5, props .5/.5 ; current [1,3,4,4] -> .25/.75
    expected = (0.25 - 0.5) * math.log(0.25 / 0.5) + (0.75 - 0.5) * math.log(0.75 / 0.5)
    assert psi([1, 2, 3, 4], [1, 3, 4, 4], bins=2) == pytest.approx(expected, abs=1e-9)
    assert psi([1, 2, 3, 4], [1, 2, 3, 4], bins=2) == pytest.approx(0.0, abs=1e-12)


def test_psi_out_of_range_lands_in_extreme_bins() -> None:
    assert psi([1, 2, 3, 4], [100, 200, 300, 400], bins=2) > 1.0


def test_ks_pvalue_known_points() -> None:
    assert ks_pvalue(0.0, 100, 100) == 1.0
    assert ks_pvalue(1.0, 100, 100) < 1e-10
    assert ks_pvalue(0.5, 5, 5) is None  # too small for the asymptotic formula
    p = ks_pvalue(0.1, 200, 200)
    assert p is not None and 0.2 < p < 0.4  # lambda ~ 1.0 -> p ~ 0.27


# --- same law / shift ------------------------------------------------------------------------


def test_same_law_false_positive_rates() -> None:
    """Measured honestly: PSI noise ~ (bins-1)(1/n+1/m); see module docstring."""
    big = [detect_drift(gauss(1000, s), gauss(1000, 1000 + s)) for s in range(20)]
    assert sum(r.psi < 0.1 for r in big if r.psi is not None) / 20 >= 0.95
    assert not any(r.psi_level == "strong" or r.drifted for r in big)
    small = [detect_drift(gauss(200, s), gauss(200, 1000 + s)) for s in range(20)]
    assert not any(r.psi_level == "strong" for r in small)
    assert sum(r.drifted for r in small) <= 2  # default rule: <= 10 % false alerts at n=200


def test_mean_shift_raises_psi_monotonically_and_is_significant() -> None:
    ref = gauss(500, 1)
    last = -1.0
    for shift in (0.0, 0.5, 1.0, 2.0):
        res = detect_drift(ref, gauss(500, 2, mu=shift))
        assert res.psi is not None and res.psi > last
        last = res.psi
        if shift >= 1.0:
            assert res.psi_level == "strong" and res.drifted
            assert res.ks_pvalue is not None and res.ks_pvalue < 0.001
    assert not detect_drift(ref, gauss(500, 2)).drifted


def test_thresholds_configurable() -> None:
    ref, cur = gauss(500, 1), gauss(500, 2, mu=0.5)
    lax = detect_drift(ref, cur, config=DriftConfig(psi_moderate=0.9, psi_strong=2.0))
    assert lax.psi_level == "stable" and not lax.drifted
    with pytest.raises(ValueError):
        DriftConfig(psi_moderate=0.3, psi_strong=0.2)


# --- edge cases ------------------------------------------------------------------------------


def test_edge_cases() -> None:
    assert detect_drift([], []).skipped_reason == "too_few_samples"
    assert detect_drift([None] * 50, gauss(50, 1)).skipped
    assert detect_drift(gauss(29, 1), gauss(100, 2)).skipped
    assert not detect_drift(gauss(30, 1), gauss(30, 2)).skipped
    with pytest.raises(QualityError):
        psi([], [1.0])
    with pytest.raises(QualityError):
        ks_statistic([1.0], [])
    assert psi([5.0], [5.0]) == pytest.approx(0.0, abs=1e-12)
    junk: list[object] = [math.nan, math.inf, "x", True, None]
    assert psi([*junk, 1.0, 2.0, 3.0], [*junk, 1.0, 2.0, 3.0], bins=2) == pytest.approx(
        0.0, abs=1e-12
    )
    with pytest.raises(QualityError):
        detect_drift([1.0] * 11, [1.0] * 11, config=DriftConfig(max_items=10))


def test_massive_repeats_degenerate_bins() -> None:
    same = detect_drift([0.0] * 100, [0.0] * 100)
    assert same.psi == pytest.approx(0.0, abs=1e-12) and not same.drifted
    moved = detect_drift([0.0] * 100, [0.0] * 50 + [9.0] * 50)
    assert moved.psi_level == "strong" and moved.drifted
    mostly = [0.0] * 90 + gauss(10, 3)
    assert psi(mostly, mostly) == pytest.approx(0.0, abs=1e-12)


def test_categorical_new_and_disappeared() -> None:
    ref = ["a"] * 40 + ["b"] * 40 + ["c"] * 20
    cur = ["a"] * 40 + ["b"] * 40 + ["z"] * 20
    value, new, gone = categorical_drift(ref, cur)
    assert (new, gone) == (1, 1) and value > 0.25
    res = detect_drift(ref, cur, kind="categorical", column="region")
    assert res.drifted and res.new_categories == 1 and res.disappeared_categories == 1
    assert not detect_drift(ref, ref, kind="categorical").drifted
    assert detect_drift(ref[:10], cur[:10], kind="categorical").skipped
    with pytest.raises(QualityError):
        categorical_drift([], ["a"])


# --- fixtures --------------------------------------------------------------------------------


def cleaned(path: Path):
    table = read_file(path)
    cands = map_columns(table.columns, DICT)
    mapping = {c: m[0].target for c, m in cands.items() if m}
    return clean_table(table, mapping, dictionary=DICT, transforms=TRANSFORMS, text_rules=RULES,
                       source="d", run_id="r", now=lambda: T0)  # fmt: skip


def col(res, name: str) -> list[object]:
    j = res.columns.index(name)
    return [r[j] for r in res.rows]


def test_fixtures_v1_v2_v3_evaluated_at_n_48() -> None:
    d = ROOT / "tests" / "data" / "drift"
    v1, v2, v3 = (cleaned(d / v / "population.csv") for v in ("v1", "v2", "v3"))
    # n = 48 >= 30: evaluated, not skipped. v3 is v1 after cleaning -> identical, no drift.
    r13 = detect_drift(col(v1, "population_total"), col(v3, "population_total"))
    assert not r13.skipped and r13.psi == pytest.approx(0.0, abs=1e-12) and not r13.drifted
    assert r13.ks_stat == 0.0
    # v1 vs v2 share the same base values (the simulator only changes the schema): no drift.
    r12 = detect_drift(col(v1, "unemployment_rate"), col(v2, "unemployment_rate"))
    assert not r12.skipped and not r12.drifted
    # Below min_samples=100 (custom) the same data is skipped: the correct behaviour.
    cfg = DriftConfig(min_samples=100)
    assert detect_drift(
        col(v1, "population_total"), col(v3, "population_total"), config=cfg
    ).skipped


def test_simulator_larger_set_and_scaled_population(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "drift_sim", ROOT / "scripts" / "drift_simulator.py"
    )
    assert spec and spec.loader
    sim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sim)
    sim.generate(42, tmp_path, years=10)
    res = cleaned(tmp_path / "v1" / "population.csv")
    pop = col(res, "population_total")
    assert len(pop) == 120
    assert not detect_drift(pop, pop).drifted
    scaled = [p * 1.5 for p in pop]  # type: ignore[operator]
    assert detect_drift(pop, scaled).drifted


# --- no data values in results ---------------------------------------------------------------


def test_no_sentinel_value_in_result() -> None:
    sentinel = 987654.321
    ref = [sentinel] + gauss(60, 1)
    dump = detect_drift(ref, gauss(60, 2)).model_dump_json()
    assert "987654" not in dump
    cats = detect_drift(
        ["SECRET_CAT"] * 40, ["SECRET_CAT"] * 20 + ["OTHER_X"] * 20, kind="categorical"
    )
    assert "SECRET" not in cats.model_dump_json() and "OTHER_X" not in cats.model_dump_json()


# --- properties ------------------------------------------------------------------------------

nums = st.lists(st.floats(min_value=-1e6, max_value=1e6, allow_nan=False), min_size=1, max_size=60)


@settings(derandomize=True, database=None, max_examples=120, deadline=None)
@given(a=nums, b=nums)
def test_psi_ks_properties(a: list[float], b: list[float]) -> None:
    assert psi(a, b) >= 0.0
    assert psi(a, a) == pytest.approx(0.0, abs=1e-9)
    d = ks_statistic(a, b)
    assert 0.0 <= d <= 1.0
    assert d == ks_statistic(b, a)
    assert ks_statistic(a, a) == 0.0
