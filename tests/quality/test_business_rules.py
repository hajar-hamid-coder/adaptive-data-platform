"""Business rules (P4-06): whitelisted expressions, attack refusals, sum_equals, integration."""

from datetime import date, datetime
from pathlib import Path

import pytest
from pydantic import TypeAdapter
from structlog.testing import capture_logs

from adp.errors import ConfigError
from adp.mapping.dictionary import load_dictionary
from adp.quality.quarantine import QuarantineConfig, decide
from adp.quality.rules import Rule, evaluate, load_references, load_rules
from adp.quality.score import ScoreConfig, score_batch

ROOT = Path(__file__).resolve().parents[2]
QDIR = ROOT / "config" / "quality"
DICT = load_dictionary(ROOT / "config" / "semantic" / "canonical.yaml")
RULE = TypeAdapter(Rule)


def expr(text: str, cols: list[str], **kw: object) -> Rule:
    return RULE.validate_python(
        {"id": "r1", "type": "expression", "columns": cols, "expression": text, **kw}
    )


def run(rule: Rule, cols: list[str], rows: list[list], **kw):
    rep = evaluate([rule], cols, rows, **kw)
    return rep, rep.results[0]


D = date


def test_date_order_violation_identifies_rows() -> None:
    rule = expr("date_fin >= date_debut", ["date_debut", "date_fin"])
    rows = [
        [D(2020, 1, 1), D(2020, 6, 1)],
        [D(2020, 5, 1), D(2020, 1, 1)],  # violation
        [D(2020, 1, 1), D(2020, 1, 1)],
        [D(2021, 1, 1), D(2020, 1, 1)],  # violation
    ]
    rep, res = run(rule, ["date_debut", "date_fin"], rows)
    assert (res.status, res.failed_rows, res.evaluated, res.level) == ("failed", [1, 3], 4, "row")
    assert rep.failed_row_indices == [1, 3] and not rep.ok
    assert "2020" not in res.message


def test_total_equals_sum_of_parts() -> None:
    rule = expr("total = a + b", ["total", "a", "b"])
    _, res = run(rule, ["total", "a", "b"], [[3, 1, 2], [5, 1, 2], [3.0, 1.5, 1.5]])
    assert res.failed_rows == [1]


def test_null_is_unknown_not_failure() -> None:
    rule = expr("a < b", ["a", "b"])
    _, res = run(rule, ["a", "b"], [[1, 2], [None, 2], [3, None], [5, 2]])
    assert (res.evaluated, res.failed_rows) == (2, [3])


def test_three_valued_logic_and_functions() -> None:
    cols = ["a", "b"]
    rows = [[None, 1], [2, None], [None, None]]
    assert run(expr("COALESCE(a, b, 0) >= 0", cols), cols, rows)[1].failed_count == 0
    assert run(expr("a IS NULL AND b IS NOT NULL", cols), cols, rows)[1].failed_rows == [1, 2]
    # NULL OR TRUE is TRUE; NULL AND FALSE is FALSE
    assert run(expr("a > 0 OR b = 1", cols), cols, rows)[1].failed_rows == []
    assert run(expr("NOT (a > 0 AND b = 99)", cols), cols, [[None, 1]])[1].failed_rows == []
    assert run(expr("ABS(a - b) <= 1.5 AND NOT a = 9", cols), cols, [[1, 2], [5, 1]])[
        1
    ].failed_rows == [1]
    assert run(expr("CAST(a AS INTEGER) = 2", cols), cols, [["2", 0], ["x", 0]])[1].failed_rows == [
        1
    ]
    assert run(expr("name <> 'it''s' AND d > DATE '2020-01-01'", ["name", "d"]), ["name", "d"],
               [["ok", D(2021, 1, 1)], ["it's", D(2021, 1, 1)],
                ["ok", datetime(2019, 1, 1)]])[1].failed_rows == [1, 2]  # fmt: skip


def test_not_evaluable_rows_fail_without_leaking_values() -> None:
    cols = ["a", "b"]
    rule = expr("a / b > 1", cols)
    rows = [[4, 2], [4, 0], ["SECRET-VALUE", 2], [True, 2]]
    with capture_logs() as logs:
        _, res = run(rule, cols, rows)
    assert res.failed_rows == [1, 2, 3]  # zero division, text, bool -> violation
    assert "SECRET" not in res.message and "SECRET" not in repr(res) and "SECRET" not in repr(logs)
    # comparing a date column with a number is also "not evaluable"
    _, res = run(expr("d > 1", ["d"]), ["d"], [[D(2020, 1, 1)]])
    assert res.failed_rows == [0]


def test_severity_warn_and_absent_column() -> None:
    rule = expr("a > b", ["a", "b"], severity="warn")
    rep, res = run(rule, ["a", "b"], [[1, 2]])
    assert res.status == "failed" and rep.failed_row_indices == [] and rep.ok
    _, skipped = run(rule, ["a"], [[1]])
    assert skipped.status == "skipped"
    with pytest.raises(ConfigError):
        evaluate([rule], ["a"], [[1]], strict=True)


# --- attacks / refusals at config validation ----------------------------------------------------

ATTACKS = [
    "a > 0; DROP TABLE t",
    "a > 0 -- comment",
    "a > 0 /* c */",
    "a IN (SELECT 1)",
    "(SELECT 1) = a",
    "a = (SELECT b FROM t)",
    "read_csv('/etc/passwd') = a",
    "length(read_text('/etc/passwd')) > a",
    "getenv('HOME') = 'x'",
    "a > 0 UNION SELECT 1",
    "COPY t TO '/tmp/x'",
    "ATTACH '/tmp/x.db'",
    "PRAGMA enable_external_access",
    "INSTALL httpfs",
    "LOAD httpfs",
    "glob('/*') = a",
    "a = 'x' || 'y'",
    'a = "b"',
    "a = `b`",
    "a LIKE 'x%'",
    "a > unknown_col",
    "upper(a) = 'X'",
    "CAST(a AS BLOB) = 1",
    "a + b",  # not a predicate
    "a > ",
    "",
    "   ",
    "a > 1)",
    "(a > 1",
    "a > 1 AND b",  # AND operand not boolean
    "a = DATE '2020-13-45'",
    "a = é",
    "a > 0\x00",
    "a > 1 OR " * 40 + "a > 1",  # too long
    "(" * 30 + "a > 1" + ")" * 30,  # too deep
    "ABS(a, b) > 1",
    "COALESCE() > 1",
]


@pytest.mark.parametrize("text", ATTACKS)
def test_attacks_and_invalid_expressions_refused_at_validation(text: str) -> None:
    with pytest.raises(ValueError):
        expr(text, ["a", "b"])


def test_load_refuses_attack_with_config_error(tmp_path: Path) -> None:
    f = tmp_path / "r.yaml"
    f.write_text(
        "rules:\n  - {id: x, type: expression, columns: [a],"
        " expression: \"read_csv('/etc/passwd') = a\"}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as ei:
        load_rules(f)
    assert "passwd" not in str(ei.value) and "x" in str(ei.value)


def test_expression_columns_must_be_in_glossary(tmp_path: Path) -> None:
    f = tmp_path / "r.yaml"
    f.write_text(
        "rules:\n  - {id: x, type: expression, columns: [nope, year], expression: 'nope > year'}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="not in the glossary"):
        load_rules(f, DICT)


def test_no_sql_engine_or_filesystem_reachable() -> None:
    """The evaluator is pure Python: nothing in the module can reach DuckDB, files or env."""
    import adp.quality.expression as e

    src = Path(e.__file__).read_text(encoding="utf-8")
    for banned in ("duckdb", "eval(", "exec(", "open(", "os.", "subprocess", "__import__"):
        assert banned not in src.replace("evaluate_predicate", "")


# --- sum_equals -----------------------------------------------------------------------------


def sum_rule(tol: float = 0, **kw: object) -> Rule:
    return RULE.validate_python(
        {"id": "s1", "type": "sum_equals", "column": "pop", "reference_file": "nat",
         "reference_field": "total", "tolerance": tol, **kw}
    )  # fmt: skip


def test_sum_equals_batch_verdict_with_tolerance() -> None:
    rows = [[10], [20], [None], [12.5]]
    refs = {"nat:total": [42]}
    rep, res = run(sum_rule(), ["pop"], rows, references={"nat:total": [42]})
    assert (res.status, res.level, res.failed_count, res.failed_rows) == ("failed", "batch", 1, [])
    assert rep.failed_row_indices == [] and not rep.ok
    assert "42" not in res.message and "42" not in str(res.detail.values())
    assert run(sum_rule(0.5), ["pop"], rows, references=refs)[1].status == "passed"
    assert run(sum_rule(), ["pop"], [[40], [2]], references=refs)[1].status == "passed"
    assert (
        run(sum_rule(), ["pop"], [[40], [1], [1]], references={"nat:total": [40, 2]})[1].status
        == "passed"
    )  # several referential values are added


def test_sum_equals_skips_and_non_numeric() -> None:
    assert run(sum_rule(), ["pop"], [[1]])[1].status == "skipped"  # no referential
    assert run(sum_rule(), ["pop"], [[1]], references={"nat:total": ["a"]})[1].status == "skipped"
    assert run(sum_rule(), ["other"], [[1]], references={"nat:total": [1]})[1].status == "skipped"
    bad = run(sum_rule(), ["pop"], [[1], ["x"]], references={"nat:total": [1]})[1]
    assert bad.status == "failed"  # a non-numeric cell fails the batch


def test_sum_equals_references_loaded_from_directory(tmp_path: Path) -> None:
    (tmp_path / "nat.yaml").write_text("total: [30]\n", encoding="utf-8")
    refs = load_references([sum_rule()], tmp_path)
    assert refs == {"nat:total": [30]}
    assert run(sum_rule(), ["pop"], [[10], [20]], references=refs)[1].status == "passed"


@pytest.mark.parametrize("bad", [{"tolerance": -1}, {"extra": 1}, {"column": "Pop"}])
def test_sum_equals_model_validation(bad: dict) -> None:
    with pytest.raises(ValueError):
        sum_rule(**bad)


# --- shipped config + integration with score / quarantine -----------------------------------


def test_shipped_business_rules_load_and_run() -> None:
    rules = load_rules(QDIR / "business_rules.yaml", DICT)
    assert {r.type for r in rules} == {"expression"} and len(rules) >= 3
    cols = ["population_total", "year", "gdp_current_usd", "gdp_per_capita_usd", "period"]
    rows = [
        [1000, 2020, 5000.0, 5.0, D(2020, 1, 1)],
        [1000, None, 5000.0, 5.0, D(2020, 1, 1)],  # population without year
        [1000, 2020, 9000.0, 5.0, D(2020, 1, 1)],  # incoherent GDP (warn)
        [1000, 2020, 5000.0, 5.0, D(1800, 1, 1)],  # implausible period
    ]
    rep = evaluate(rules, cols, rows)
    by = {r.rule_id: r for r in rep.results}
    assert by["population_requires_year"].failed_rows == [1]
    assert by["gdp_per_capita_consistent"].failed_rows == [2]
    assert by["observation_period_plausible"].failed_rows == [3]
    assert rep.failed_row_indices == [1, 3]  # the warn rule does not quarantine


def test_works_with_score_and_quarantine() -> None:
    rules = [expr("date_fin >= date_debut", ["date_debut", "date_fin"]), sum_rule()]
    cols = ["date_debut", "date_fin", "pop"]
    rows = [[D(2020, 1, 1), D(2020, 2, 1), 1], [D(2020, 3, 1), D(2020, 2, 1), 1]]
    rep = evaluate(rules, cols, rows, references={"nat:total": [2]})
    sc = score_batch(cols, rows, rep, rules, config=ScoreConfig())
    assert sc.score is not None
    assert {c.column for c in sc.columns} == set(cols)
    decision = decide(
        rep, QuarantineConfig(alert_rate=0.05, batch_rate=0.9, critical_score=None), sc
    )
    assert decision.level == "row"
