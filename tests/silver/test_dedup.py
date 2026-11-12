"""Silver deduplication (P3-07): exact + blocked fuzzy duplicates, survivorship, report."""

import json
import random
import time
from datetime import date

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

from adp.errors import ConfigError, SchemaError
from adp.silver.dedup import DedupConfig, dedupe, render_report

REGIONS = [
    "Tanger-Tétouan-Al Hoceïma", "L'Oriental", "Fès-Meknès", "Rabat-Salé-Kénitra",
    "Béni Mellal-Khénifra", "Casablanca-Settat", "Marrakech-Safi", "Drâa-Tafilalet",
    "Souss-Massa", "Guelmim-Oued Noun", "Laâyoune-Sakia El Hamra", "Dakhla-Oued Ed-Dahab",
]  # fmt: skip
INDICATORS = [
    "population_total", "unemployment_rate", "activity_rate", "inflation_rate", "literacy_rate",
    "life_expectancy", "gdp_per_capita_usd", "gdp_current_usd",
]  # fmt: skip
COLS = ["region", "annee", "indicator", "valeur"]
KEY = ["region", "annee", "indicator"]


def synthetic(n_years: int, n_ind: int, limit: int | None = None) -> list[list]:
    """regions x years x indicators, distinct keys, seeded values (synthetic only)."""
    rng = random.Random(42)
    rows = [
        [r, 2000 + y, ind, round(rng.uniform(0, 100), 2)]
        for r in REGIONS
        for y in range(n_years)
        for ind in INDICATORS[:n_ind]
    ]
    rng.shuffle(rows)
    return rows[:limit] if limit else rows


def variant(name: str, kind: str) -> str:
    if kind == "case":
        return name.upper()
    if kind == "accents":
        return "  " + name.lower().replace("é", "e").replace("ï", "i").replace("â", "a") + " "
    if kind == "hyphens":
        return name.replace("-", " ").replace("'", " ").lower()
    return name[:4] + name[5:]  # typo: one dropped letter


def injected(rows: list[list]) -> tuple[list[list], set[tuple[int, int]]]:
    rng = random.Random(7)
    kinds = ["exact"] * 3 + ["case", "accents", "accents"] + ["hyphens"] * 2 + ["typo"] * 2
    srcs = rng.sample(range(len(rows)), 10)
    out = [list(r) for r in rows]
    expected = set()
    for k, s in zip(kinds, srcs, strict=True):
        dup = list(rows[s])
        if k != "exact":
            dup[0] = variant(dup[0], k)
        out.append(dup)
        expected.add((s, len(out) - 1))
    return out, expected


def test_backlog_10_duplicates_over_1000_rows() -> None:
    base = synthetic(17, 5, limit=990)
    rows, expected = injected(base)
    assert len(rows) == 1000
    t0 = time.perf_counter()
    res = dedupe(COLS, rows, key_fields=KEY)
    elapsed = time.perf_counter() - t0
    found = {g.indices for g in res.groups}
    assert found == expected  # 10/10 recalled, 0 false positive (no other group, no extra member)
    assert res.report.eliminated == 10 and len(res.survivors) == 990
    assert elapsed < 1.0
    print(f"dedup 1000 rows: {elapsed * 1000:.0f} ms, {res.report.comparisons} comparisons")


def test_blocking_is_not_quadratic() -> None:
    rows = synthetic(52, 8)  # 4992 rows
    assert len(rows) > 4900
    rows, expected = injected(rows)
    t0 = time.perf_counter()
    res = dedupe(COLS, rows, key_fields=KEY)
    elapsed = time.perf_counter() - t0
    assert {g.indices for g in res.groups} == expected
    assert res.report.comparisons < len(rows) ** 2 / 2 / 50
    assert elapsed < 5.0


def test_time_series_is_not_duplicate() -> None:
    rows = [[r, y, 1.0] for r in REGIONS for y in range(2015, 2025)]
    res = dedupe(["region", "annee", "v"], rows, key_fields=["region", "annee"])
    assert res.groups == [] and len(res.survivors) == 120


def test_distinct_close_names_stay_distinct() -> None:
    names = ["Drâa-Tafilalet", "Souss-Massa", "Fès-Meknès", "Béni Mellal-Khénifra"]
    res = dedupe(["r", "y"], [[n, 2020] for n in names], key_fields=["r", "y"])
    assert res.groups == []


def test_digits_make_codes_distinct() -> None:
    rows = [["indicator_00001", 1], ["indicator_00002", 1]]
    assert dedupe(["c", "y"], rows, key_fields=["c", "y"]).groups == []


def test_null_keys_never_grouped() -> None:
    rows = [
        [None, 2020, 1],
        [None, 2020, 1],
        ["A", None, 1],
        ["A", None, 1],
        ["", 2020, 1],
        ["", 2020, 1],
    ]
    res = dedupe(["r", "y", "v"], rows, key_fields=["r", "y"])
    assert res.groups == [] and len(res.survivors) == 6
    # one null key vs one valid key: not merged either
    res = dedupe(["r", "y"], [["Fès", 2020], [None, 2020]], key_fields=["r", "y"])
    assert res.groups == []


def test_exact_variants_group_as_exact() -> None:
    rows = [["Fès-Meknès", 2020], ["FES  MEKNES", 2020]]
    g = dedupe(["r", "y"], rows, key_fields=["r", "y"]).groups
    assert [(x.method, x.score) for x in g] == [("exact", 1.0)]


def test_fuzzy_group_and_review_pairs_not_merged() -> None:
    cols = ["r", "y"]
    res = dedupe(cols, [["Marrakech-Safi", 2020], ["Marrakech-Saf", 2020]], key_fields=cols)
    assert [g.method for g in res.groups] == ["fuzzy"] and res.groups[0].score >= 0.9
    # "literacy rate" vs "activity rate" sit in the review band: reported, never merged
    rows = [["literacy_rate", 1], ["activity_rate", 1]]
    res = dedupe(cols, rows, key_fields=cols)
    assert res.groups == [] and len(res.survivors) == 2
    assert [(p.a, p.b) for p in res.report.review_pairs] == [(0, 1)]
    assert 0.6 <= res.report.review_pairs[0].score < 0.9


def test_hard_field_mismatch_blocks_fuzzy() -> None:
    rows = [["Marrakech-Safi", 2020], ["Marrakech-Saf", 2021]]
    assert dedupe(["r", "y"], rows, key_fields=["r", "y"]).groups == []


def test_survivorship_most_complete_then_latest_then_first() -> None:
    cols = ["k", "a", "b", "d"]
    d1, d2 = date(2020, 1, 1), date(2021, 1, 1)
    cfg = DedupConfig(sort_field="d")
    # most complete wins even if older
    rows = [["x", 1, None, d2], ["x", 1, 2, d1]]
    assert dedupe(cols, rows, key_fields=["k"], config=cfg).survivor_indices == [1]
    # tie on completeness -> most recent
    rows = [["x", 1, 2, d1], ["x", 1, 2, d2]]
    assert dedupe(cols, rows, key_fields=["k"], config=cfg).survivor_indices == [1]
    # tie on both -> first; without sort_field -> first
    rows = [["x", 1, 2, d1], ["x", 3, 4, d1], ["x", 5, 6, d1]]
    assert dedupe(cols, rows, key_fields=["k"], config=cfg).survivor_indices == [0]
    assert dedupe(cols, rows, key_fields=["k"]).survivors == [rows[0]]


def test_coalesce_fills_nulls_and_keeps_index() -> None:
    cols = ["k", "a", "b"]
    rows = [["y", 9, 9], ["x", 1, None], ["x", None, 2], ["x", 7, 8]]
    plain = dedupe(cols, rows, key_fields=["k"])
    assert plain.survivors[1] == ["x", 7, 8]
    rows = [["x", 1, None], ["x", None, 2], ["z", 0, 0]]
    res = dedupe(cols, rows, key_fields=["k"], config=DedupConfig(coalesce=True))
    assert res.survivor_indices == [0, 2] and res.survivors[0] == ["x", 1, 2]
    assert rows[0] == ["x", 1, None]  # input untouched


def test_deterministic_and_invariant() -> None:
    rows, _ = injected(synthetic(17, 5, limit=990))
    a = dedupe(COLS, rows, key_fields=KEY)
    b = dedupe(COLS, [list(r) for r in rows], key_fields=KEY)
    assert a.report.to_json() == b.report.to_json() and a.survivors == b.survivors
    rep = a.report
    assert rep.survivors + rep.eliminated == rep.rows_read
    accounted = {i for g in rep.groups for i in g.indices if i != g.survivor}
    assert len(accounted) == rep.eliminated
    assert set(a.survivor_indices).isdisjoint(accounted)
    assert set(a.survivor_indices) | accounted == set(range(rep.rows_read))
    assert all(g.survivor in g.indices for g in rep.groups)


def test_config_validation() -> None:
    for kw in ({"auto_threshold": 1.2}, {"review_threshold": 0.95}, {"block_prefix": 0}):
        with pytest.raises(ConfigError):
            DedupConfig(**kw)
    # looser threshold merges what the default only flags
    rows = [["literacy_rate", 1], ["activity_rate", 1]]
    cfg = DedupConfig(auto_threshold=0.6, review_threshold=0.5)
    assert len(dedupe(["c", "y"], rows, key_fields=["c", "y"], config=cfg).groups) == 1


def test_empty_and_errors() -> None:
    res = dedupe(["a"], [], key_fields=["a"])
    assert res.survivors == [] and res.report.rows_read == 0
    with pytest.raises(SchemaError, match="absent"):
        dedupe(["a"], [["x"]], key_fields=["nope"])
    with pytest.raises(SchemaError, match="absent"):
        dedupe(["a"], [["x"]], key_fields=["a"], config=DedupConfig(sort_field="zz"))
    with pytest.raises(ConfigError):
        dedupe(["a"], [["x"]], key_fields=[])
    with pytest.raises(ConfigError):
        dedupe(["a"], [["x"]], key_fields=["a", "a"])
    with pytest.raises(SchemaError):
        dedupe(["a", "b"], [["x"]], key_fields=["a"])


def test_no_data_value_in_report_or_logs() -> None:
    s = "SENTINEL-ZQXJ"
    rows = [[s, 2020, "v1"], [s.lower(), 2020, "v2"], [s[:-1], 2020, "v3"]]
    with capture_logs() as logs:
        res = dedupe(["r", "y", "v"], rows, key_fields=["r", "y"])
    blob = render_report(res.report) + res.report.to_json() + json.dumps(logs, default=str)
    assert "zqxj" not in blob.lower() and "v1" not in blob
    assert res.report.groups and "survivante" in render_report(res.report)


@settings(max_examples=150, derandomize=True, database=None, deadline=None)
@given(
    st.lists(
        st.tuples(
            st.one_of(
                st.none(), st.sampled_from(["a b", "A-B", "ab", "abc", "xyz", "", "x1", "x2"])
            ),
            st.one_of(st.none(), st.integers(2018, 2020)),
            st.one_of(st.none(), st.integers(0, 3)),
        ),
        max_size=25,
    ),
    st.booleans(),
)
def test_idempotent_and_never_grows(raw: list[tuple], coalesce: bool) -> None:
    rows = [list(r) for r in raw]
    cfg = DedupConfig(coalesce=coalesce)
    first = dedupe(["k", "y", "v"], rows, key_fields=["k", "y"], config=cfg)
    assert len(first.survivors) <= len(rows)
    again = dedupe(["k", "y", "v"], first.survivors, key_fields=["k", "y"], config=cfg)
    assert again.survivors == first.survivors and again.groups == []
