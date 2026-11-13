"""Quality rules engine (P4-01): each rule type passing/failing, safe regex, loading, v1 data."""

import json
import time
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter
from structlog.testing import capture_logs

from adp.connectors.files import read_file
from adp.errors import ConfigError
from adp.mapping.dictionary import load_dictionary
from adp.mapping.rules import map_columns
from adp.mapping.transform import load_field_transforms
from adp.quality import rules as qr
from adp.quality.rules import (
    Rule,
    check_pattern,
    evaluate,
    load_reference,
    load_references,
    load_rules,
)
from adp.silver.clean import clean_table, load_cleaning

ROOT = Path(__file__).resolve().parents[2]
SEM = ROOT / "config" / "semantic"
QDIR = ROOT / "config" / "quality"
DICT = load_dictionary(SEM / "canonical.yaml")
RULE = TypeAdapter(Rule)


def mk(**kw: object) -> Rule:
    return RULE.validate_python({"id": "r1", **kw})


def run(rule: Rule, cols: list[str], rows: list[list], **kw):
    rep = evaluate([rule], cols, rows, **kw)
    return rep, rep.results[0]


# --- not_null / unique -------------------------------------------------------------------------


def test_not_null() -> None:
    _, ok = run(mk(type="not_null", column="a"), ["a"], [[1], [2]])
    assert (ok.status, ok.failed_count, ok.evaluated) == ("passed", 0, 2)
    rep, bad = run(mk(type="not_null", column="a"), ["a"], [[1], [None], [3], [None]])
    assert (bad.status, bad.failed_count, bad.failed_rows, bad.evaluated) == (
        "failed",
        2,
        [1, 3],
        4,
    )
    assert rep.failed_row_indices == [1, 3] and not rep.ok


def test_unique_single_and_composite_with_nulls() -> None:
    cols = ["a", "b"]
    rows = [[1, "x"], [1, "y"], [1, "x"], [2, "x"], [1, "x"]]
    _, one = run(mk(type="unique", columns=["a"]), cols, rows)
    assert one.failed_rows == [1, 2, 4] and one.failed_count == 3
    _, comp = run(mk(type="unique", columns=["a", "b"]), cols, rows)
    assert comp.failed_rows == [2, 4] and comp.evaluated == 5  # first occurrence is kept
    _, passing = run(mk(type="unique", columns=["a", "b"]), cols, rows[:2] + rows[3:4])
    assert passing.status == "passed"
    nulls = [[None, "x"], [None, "x"], [1, "x"], [1, "x"]]
    _, ign = run(mk(type="unique", columns=["a", "b"]), cols, nulls)
    assert ign.failed_rows == [3] and ign.evaluated == 2
    _, keep = run(mk(type="unique", columns=["a", "b"], ignore_null_keys=False), cols, nulls)
    assert keep.failed_rows == [1, 3]


# --- range -------------------------------------------------------------------------------------


def test_range_numeric_bounds_and_types() -> None:
    rule = mk(type="range", column="a", min=0, max=100)
    rows = [[0], [100], [50.5], [-1], [100.1], [None], ["abc"], [True], [float("nan")]]
    _, r = run(rule, ["a"], rows)
    assert r.failed_rows == [3, 4, 6, 7, 8] and r.evaluated == 8  # None ignored, bad types fail
    _, ex = run(
        mk(type="range", column="a", min=0, max=100, inclusive=False), ["a"], [[0], [100], [5]]
    )
    assert ex.failed_rows == [0, 1]
    _, lo = run(mk(type="range", column="a", min=0), ["a"], [[10**9], [-1]])
    assert lo.failed_rows == [1]
    _, hi = run(mk(type="range", column="a", max=5), ["a"], [[-(10**9)], [6]])
    assert hi.failed_rows == [1]


def test_range_dates() -> None:
    rule = mk(type="range", column="d", min=date(2020, 1, 1), max=date(2020, 12, 31))
    rows = [[date(2020, 1, 1)], [datetime(2020, 12, 31, 23, 0, tzinfo=UTC)], [date(2019, 12, 31)],
            [date(2021, 1, 1)], [5], [None]]  # fmt: skip
    _, r = run(rule, ["d"], rows)
    assert r.failed_rows == [2, 3, 4] and r.evaluated == 5


# --- regex -------------------------------------------------------------------------------------


def test_regex_match_fail_nonstr_and_long() -> None:
    rule = mk(type="regex", column="c", pattern="^[A-Z]{3}$")
    rows = [["MAR"], ["maroc"], [None], [123], ["FRAA"], ["X" * 100_000]]
    t0 = time.perf_counter()
    _, r = run(rule, ["c"], rows)
    assert time.perf_counter() - t0 < 1
    assert r.failed_rows == [1, 3, 4, 5] and r.evaluated == 5
    _, nums = run(mk(type="regex", column="c", pattern=r"^\d+\.\d$"), ["c"], [[16.1], [3]])
    assert nums.failed_rows == [1]  # non-str values are converted with str()


def test_long_value_does_not_run_the_regex() -> None:
    class Boom:
        def fullmatch(self, _: str) -> None:
            raise AssertionError("regex executed")

    assert qr._matches(Boom(), "x" * (qr.MAX_VALUE_LEN + 1)) is False  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "pattern",
    ["(a+)+$", "(a*)*", "(a|aa)+", r"(a)\1", "(?=a)a", "(?!a)a", "(?<=a)b", "(a{1,3})+",
     "(\\d+\\.?)+", "(", "a" * 201],
)  # fmt: skip
def test_risky_patterns_refused_at_load(pattern: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        check_pattern(pattern)
    f = tmp_path / "q.yaml"
    f.write_text(f"rules:\n  - {{id: r, type: regex, column: c, pattern: '{pattern}'}}\n")
    with pytest.raises(ConfigError, match="q.yaml"):
        load_rules(f)


@pytest.mark.parametrize("pattern", ["^[A-Z]{3}$", r"^\d{4}-\d{2}$", "^(MAR|FRA)$", r"^[a-z]+\d*$"])
def test_safe_patterns_accepted(pattern: str) -> None:
    check_pattern(pattern)


# --- reference ---------------------------------------------------------------------------------


def test_reference_inline_and_file(tmp_path: Path) -> None:
    rows = [["A"], ["B"], ["Z"], [None], ["a"]]
    _, r = run(mk(type="reference", column="c", values=["A", "B"]), ["c"], rows)
    assert r.failed_rows == [2, 4] and r.evaluated == 4
    (tmp_path / "ref.yaml").write_text("code: [A, B]\nnum: [1, 2]\n")
    rule = mk(type="reference", column="c", reference_file="ref", reference_field="code")
    refs = load_references([rule], tmp_path)
    _, f = run(rule, ["c"], rows, references=refs)
    assert f.failed_rows == [2, 4]
    _, sk = run(rule, ["c"], rows)
    assert sk.status == "skipped"
    bad = mk(type="reference", column="c", reference_file="ref", reference_field="nope")
    with pytest.raises(ConfigError, match="nope"):
        load_references([bad], tmp_path)
    with pytest.raises(ConfigError, match="ref"):
        load_reference(tmp_path / "missing.yaml")


# --- freshness ---------------------------------------------------------------------------------


def test_freshness_as_of_injected() -> None:
    rule = mk(type="freshness", column="d", max_age_days=10)
    as_of = date(2026, 1, 31)
    cols = ["d"]
    _, edge = run(rule, cols, [[date(2026, 1, 21)], [date(2020, 1, 1)]], as_of=as_of)
    assert edge.status == "passed" and edge.detail["age_days"] == 10 and edge.level == "batch"
    rep, old = run(rule, cols, [[date(2026, 1, 20)]], as_of=datetime(2026, 1, 31, 8, tzinfo=UTC))
    assert (old.status, old.failed_count, old.detail["age_days"]) == ("failed", 1, 11)
    assert old.failed_rows == [] and rep.failed_row_indices == [] and not rep.ok
    _, empty = run(rule, cols, [[None], [None]], as_of=as_of)
    assert empty.status == "failed" and empty.detail["age_days"] is None
    _, fut = run(rule, cols, [[date(2027, 1, 1)]], as_of=as_of)
    assert fut.status == "passed"
    _, none = run(rule, cols, [[date(2026, 1, 1)]])
    assert none.status == "skipped"


# --- absent column, severity -------------------------------------------------------------------


def test_absent_column_skipped_or_strict() -> None:
    rule = mk(type="not_null", column="zzz")
    rep, r = run(rule, ["a"], [[1]])
    assert (r.status, r.evaluated, r.failed_count) == ("skipped", 0, 0) and rep.ok
    assert "zzz" in r.message
    with pytest.raises(ConfigError, match="zzz"):
        evaluate([rule], ["a"], [[1]], strict=True)


def test_warn_does_not_invalidate() -> None:
    rule = mk(type="not_null", column="a", severity="warn")
    rep, r = run(rule, ["a"], [[None], [1]])
    assert r.status == "failed" and r.severity == "warn" and r.failed_rows == [0]
    assert rep.ok and rep.failed_row_indices == []


def test_failed_rows_truncated_count_exact() -> None:
    rep, r = run(mk(type="not_null", column="a"), ["a"], [[None]] * 50, max_failed_rows=5)
    assert r.failed_count == 50 and r.failed_rows == [0, 1, 2, 3, 4]
    assert len(rep.failed_row_indices) == 50


# --- loading -----------------------------------------------------------------------------------


def write(tmp_path: Path, body: str) -> Path:
    f = tmp_path / "q.yaml"
    f.write_text(body, encoding="utf-8")
    return f


def test_load_valid_and_glossary(tmp_path: Path) -> None:
    f = write(tmp_path, "rules:\n  - {id: a, type: not_null, column: region}\n")
    assert [r.id for r in load_rules(f, DICT)] == ["a"]
    g = write(tmp_path, "rules:\n  - {id: a, type: not_null, column: nope}\n")
    with pytest.raises(ConfigError, match="glossary"):
        load_rules(g, DICT)
    assert load_rules(g)  # no dictionary: no check


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        (
            "rules:\n  - {id: a, type: not_null, column: x}\n"
            "  - {id: a, type: not_null, column: y}\n",
            "duplicate",
        ),
        ("rules:\n  - {id: a, type: sql, column: x}\n", "type"),
        ("rules:\n  - {id: a, type: not_null, column: x, extra: 1}\n", "extra"),
        ("rules:\n  - {id: a, type: range, column: x, min: 5, max: 1}\n", "min must be"),
        ("rules:\n  - {id: a, type: range, column: x}\n", "at least one"),
        ("rules:\n  - {id: A!, type: not_null, column: x}\n", "id"),
        ("rules:\n  - {id: a, type: reference, column: x}\n", "values"),
        (
            "rules:\n  - {id: a, type: reference, column: x, values: [a],\n"
            "     reference_file: f, reference_field: g}\n",
            "either",
        ),
        (
            "rules:\n  - {id: a, type: not_null, column: !!python/object/apply:os.getcwd []}\n",
            "YAML",
        ),
        ("- just a list\n", "mapping"),
    ],
)
def test_load_errors(tmp_path: Path, body: str, needle: str) -> None:
    with pytest.raises(ConfigError, match="q.yaml") as e:
        load_rules(write(tmp_path, body))
    assert needle in str(e.value)


def test_load_too_many_rules(tmp_path: Path) -> None:
    body = "rules:\n" + "".join(
        f"  - {{id: r{i}, type: not_null, column: x}}\n" for i in range(201)
    )
    with pytest.raises(ConfigError, match="rules"):
        load_rules(write(tmp_path, body))


# --- population.yaml on v1 data ----------------------------------------------------------------

SENTINEL = "ZZ_SENTINEL_VALUE"


def cleaned_v1() -> tuple[list[str], list[list]]:
    table = read_file(ROOT / "tests" / "data" / "drift" / "v1" / "population.csv")
    mapping = {c: m[0].target for c, m in map_columns(table.columns, DICT).items() if m}
    res = clean_table(
        table, mapping, dictionary=DICT,
        transforms=load_field_transforms(SEM / "transforms.yaml", dictionary=DICT),
        text_rules=load_cleaning(SEM / "cleaning.yaml", DICT),
        source="s", run_id="r", now=lambda: datetime(2026, 11, 8, tzinfo=UTC),
    )  # fmt: skip
    cols = [*res.columns, "country_code"]
    return cols, [[*r, "MAR"] for r in res.rows]


@pytest.fixture(scope="module")
def population_rules() -> tuple[list[Rule], dict]:
    rules = load_rules(QDIR / "population.yaml", DICT)
    return rules, load_references(rules, QDIR / "reference")


def by_id(rep, rid: str):
    return next(r for r in rep.results if r.rule_id == rid)


def test_regions_referential_has_12() -> None:
    assert len(load_reference(QDIR / "reference" / "regions_ma.yaml")["region"]) == 12


def test_population_config_clean_v1_zero_failure(population_rules) -> None:
    rules, refs = population_rules
    cols, rows = cleaned_v1()
    rep = evaluate(rules, cols, rows, references=refs)
    assert rep.rows_total == 48 and rep.ok and rep.failed_row_indices == []
    assert all(r.failed_count == 0 for r in rep.results)
    assert by_id(rep, "activity_pct").status == "skipped"  # column absent from v1
    assert by_id(rep, "region_in_ma_referential").evaluated == 48


def test_population_config_detects_exactly_injected_defects(population_rules) -> None:
    rules, refs = population_rules
    cols, rows = cleaned_v1()
    rows = [list(r) for r in rows]
    ix = {c: i for i, c in enumerate(cols)}
    rows[2][ix["population_total"]] = -5
    rows[3][ix["unemployment_rate"]] = 150.0
    rows[4][ix["country_code"]] = "maroc"
    rows[5][ix["region"]] = "Atlantide"
    rows[6][ix["region"]] = None
    rows.append(list(rows[0]))  # duplicate of row 0 -> index 48
    rep = evaluate(rules, cols, rows, references=refs)
    got = {r.rule_id: r.failed_rows for r in rep.results if r.failed_count}
    assert got == {
        "population_non_negative": [2],
        "unemployment_pct": [3],
        "country_code_iso3": [4],
        "region_in_ma_referential": [5],
        "region_not_null": [6],
        "region_year_unique": [48],
    }
    assert rep.failed_row_indices == [2, 3, 4, 5, 6, 48] and not rep.ok
    for r in rep.results:  # row-level invariant
        if r.level == "row":
            assert r.failed_count == len(set(r.failed_rows)) and r.failed_count <= r.evaluated


def test_no_data_value_in_report_or_logs(population_rules) -> None:
    rules, refs = population_rules
    cols, rows = cleaned_v1()
    rows[0][cols.index("region")] = SENTINEL
    rows[1][cols.index("country_code")] = SENTINEL
    with capture_logs() as logs:
        rep = evaluate(rules, cols, rows, references=refs)
    dump = rep.model_dump_json()
    assert SENTINEL not in dump and SENTINEL not in json.dumps(logs)
    assert rep.failed_row_indices == [0, 1]


def test_deterministic(population_rules) -> None:
    rules, refs = population_rules
    cols, rows = cleaned_v1()
    a = evaluate(rules, cols, rows, references=refs).model_dump_json()
    assert a == evaluate(rules, cols, rows, references=refs).model_dump_json()


# --- property ----------------------------------------------------------------------------------

CELL = st.one_of(
    st.none(), st.integers(), st.floats(allow_nan=True), st.text(max_size=20), st.booleans(),
    st.dates(), st.datetimes(),
)  # fmt: skip
ALL_RULES = [
    mk(id="r1", type="not_null", column="a"),
    mk(id="r2", type="unique", columns=["a", "b"]),
    mk(id="r3", type="range", column="a", min=0, max=10),
    mk(id="r4", type="range", column="b", min=date(2000, 1, 1)),
    mk(id="r5", type="regex", column="a", pattern="^[A-Z]{3}$"),
    mk(id="r6", type="reference", column="b", values=["x", 1]),
    mk(id="r7", type="freshness", column="a", max_age_days=5),
]


@settings(max_examples=120, derandomize=True, database=None,
          suppress_health_check=[HealthCheck.too_slow], deadline=None)  # fmt: skip
@given(rows=st.lists(st.lists(CELL, min_size=2, max_size=2), max_size=15))
def test_never_raises_on_mixed_values(rows: list[list]) -> None:
    rep = evaluate(ALL_RULES, ["a", "b"], rows, as_of=date(2026, 1, 1))
    for r in rep.results:
        assert r.failed_count <= max(r.evaluated, 1)
        assert all(0 <= i < len(rows) for i in r.failed_rows)
