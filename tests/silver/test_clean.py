"""Silver cleaning (P3-06): text rules, nulls, ISO dates, numbers, correction log, dead-letter."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

from adp.bronze.store import LocalStore
from adp.connectors.files import RawTable, read_file
from adp.errors import ConfigError, TransformError
from adp.mapping.dictionary import load_dictionary
from adp.mapping.rules import map_columns
from adp.mapping.transform import load_field_transforms
from adp.silver.clean import (
    CleaningPolicy,
    TextRule,
    clean_table,
    clean_text,
    load_cleaning,
    parse_cleaning,
    render_log,
)

ROOT = Path(__file__).resolve().parents[2]
SEM = ROOT / "config" / "semantic"
DICT = load_dictionary(SEM / "canonical.yaml")
TRANSFORMS = load_field_transforms(SEM / "transforms.yaml", dictionary=DICT)
RULES = load_cleaning(SEM / "cleaning.yaml", DICT)
DRIFT = ROOT / "tests" / "data" / "drift"
T0 = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)
TOK = frozenset({"", "-", "n/a", "null", ".."})


def run(cols: list[str], rows: list[list[str | None]], mapping: dict[str, str], **kw):
    return clean_table(RawTable(cols, rows), mapping, dictionary=DICT, transforms=TRANSFORMS,
                       text_rules=RULES, source="s", run_id="r1", now=lambda: T0, **kw)  # fmt: skip


def one(field: str, value: str | None):
    res = run(["c"], [[value]], {"c": field})
    return res.rows[0][0], res


# --- text -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("  Maroc  ", "Maroc"),
        ("tanger-tétouan-al hoceïma", "Tanger-Tétouan-Al Hoceïma"),
        ("TANGER-TÉTOUAN-AL HOCEÏMA", "Tanger-Tétouan-Al Hoceïma"),
        ("Tanger Tétouan  \t Al  Hoceïma", "Tanger Tétouan Al Hoceïma"),
        ("fès-meknès", "Fès-Meknès"),
        ("ile de france", "Ile de France"),
        ("l'oriental", "L'Oriental"),
        ("région d’azur", "Région d’Azur"),
        ("Zero​Width\x07", "Zerowidth"),
        ("été", "Été"),  # NFC composes accents
    ],
)
def test_region_text_is_normalized_accents_kept(raw: str, want: str) -> None:
    got, _ = one("region", raw)
    assert got == want


def test_country_code_upper_and_free_text_kept() -> None:
    assert one("country_code", " mar ")[0] == "MAR"
    assert one("indicator_name", " Taux  de  chômage ")[0] == "Taux de chômage"  # case kept


@pytest.mark.parametrize("raw", ["N/A", "n/a", "-", "", "null", "NULL", "..", "  ", " - "])
@pytest.mark.parametrize("field", ["region", "population_total", "unemployment_rate", "period"])
def test_sentinels_become_null(raw: str, field: str) -> None:
    got, res = one(field, raw)
    assert got is None
    assert res.corrections.counts[(field, "sentinel_to_null")] == 1


def test_dates_numbers_to_typed_values() -> None:
    for raw in ("31/01/2024", "2024-01-31", "31-01-2024", " 31/01/2024 "):
        assert one("period", raw)[0] == date(2024, 1, 31)
        assert one("period", raw)[0].isoformat() == "2024-01-31"  # type: ignore[union-attr]
    assert one("population_total", "1 080 551")[0] == 1080551
    assert one("population_total", "1 080 551")[0] == 1080551
    assert one("unemployment_rate", "16,1 %")[0] == 16.1


def test_unit_conversion_is_counted() -> None:
    ft = load_field_transforms(SEM / "transforms.yaml", dictionary=DICT)
    ft = dict(ft)
    ft["gdp_current_usd"] = type(ft["gdp_current_usd"]).model_validate(
        {
            "target": "gdp_current_usd",
            "steps": [
                {"op": "cast", "to": "float"},
                {"op": "unit_convert", "from_unit": "kUSD", "to_unit": "USD"},
            ],
        }  # fmt: skip
    )
    res = clean_table(RawTable(["g"], [["2"], ["3"]]), {"g": "gdp_current_usd"}, dictionary=DICT,
                      transforms=ft, text_rules=RULES, source="s", run_id="r")  # fmt: skip
    assert [r[0] for r in res.rows] == [2000.0, 3000.0]
    assert res.corrections.counts[("gdp_current_usd", "unit_converted")] == 2


# --- journal --------------------------------------------------------------------------------


def test_log_counts_exact_and_clean_values_not_counted() -> None:
    rows: list[list[str | None]] = [
        ["Fès-Meknès", "2020", "1 000", "10,5 %", "2020-05-01"],  # clean text, locale numbers
        ["  fès-meknès ", "2021", "N/A", "11.5", "01/05/2021"],
        ["Fès-Meknès", "2022", "1100", "-", "2022-05-01"],
    ]
    cols = ["region", "year", "pop", "chom", "d"]
    res = run(cols, rows, {"region": "region", "year": "year", "pop": "population_total",
                           "chom": "unemployment_rate", "d": "period"})  # fmt: skip
    c = res.corrections.counts
    assert c == {
        ("region", "whitespace_collapsed"): 1,
        ("region", "case_normalized"): 1,
        ("population_total", "number_locale_parsed"): 1,
        ("population_total", "sentinel_to_null"): 1,
        ("unemployment_rate", "number_locale_parsed"): 1,
        ("unemployment_rate", "sentinel_to_null"): 1,
        ("period", "date_to_iso"): 1,
    }
    assert res.corrections.rows[("region", "case_normalized")] == [2]


def test_clean_input_gives_empty_log() -> None:
    res = run(["region", "year"], [["Fès-Meknès", "2020"]], {"region": "region", "year": "year"})
    assert not res.corrections.counts
    assert render_log(res.corrections) == "Aucune correction."


def test_log_has_no_values_and_is_deterministic() -> None:
    secret = "SECRET-SENTINEL-42"
    with capture_logs() as logs:
        res = run(
            ["region", "year"],
            [[f"  {secret.lower()} ", "x"], [secret, "2020"]],
            {"region": "region", "year": "year"},
            policy=CleaningPolicy(max_reject_rate=1.1),
        )
    assert len(res.rejects) == 1
    blob = res.corrections.to_json() + render_log(res.corrections) + json.dumps(logs, default=str)
    assert secret.lower() not in blob.lower()
    assert res.corrections.to_json() == res.corrections.to_json()
    assert "casse normalisée" in render_log(res.corrections)
    assert json.loads(res.corrections.to_json())[0]["field"] == "region"


def test_sample_rows_bounded() -> None:
    res = run(["r"], [[" a "]] * 20, {"r": "region"})
    assert res.corrections.counts[("region", "whitespace_collapsed")] == 20
    assert res.corrections.rows[("region", "whitespace_collapsed")] == [1, 2, 3, 4, 5]


# --- dead-letter ----------------------------------------------------------------------------


def test_bad_rows_go_to_deadletter_with_invariant(tmp_path: Path) -> None:
    rows: list[list[str | None]] = [[f"{2000 + i}"] for i in range(9)] + [["abc"]]
    store = LocalStore(tmp_path)
    res = run(["y"], rows, {"y": "year"}, policy=CleaningPolicy(max_reject_rate=0.5),
              store=store)  # fmt: skip
    assert res.stats.rows_ok + res.stats.rows_rejected == res.stats.rows_read == 10
    assert len(res.rows) == 9
    (rj,) = res.rejects
    assert rj.line_number == 10 and rj.reason.startswith("transform_error:y:")
    assert json.loads(rj.raw_line) == ["abc"]  # original text kept
    assert res.rejects_path and (tmp_path / res.rejects_path).exists()
    assert not res.corrections.counts  # clean values, rejected row not counted


def test_reject_threshold_raises() -> None:
    with pytest.raises(TransformError, match="rejected"):
        run(["y"], [["2020"], ["x"], ["2021"], ["2022"], ["y"]], {"y": "year"})  # 40 %


def test_rejected_rows_do_not_feed_the_log() -> None:
    rows: list[list[str | None]] = [[" a ", "x"]] + [[" a ", "2020"]] * 9
    res = run(["r", "y"], rows, {"r": "region", "y": "year"},
              policy=CleaningPolicy(max_reject_rate=0.5))  # fmt: skip
    assert res.corrections.counts[("region", "whitespace_collapsed")] == 9


# --- idempotence / properties -----------------------------------------------------------------


@settings(derandomize=True, database=None, max_examples=200, deadline=None)
@given(st.text(max_size=40), st.sampled_from(["keep", "upper", "lower", "title"]))
def test_clean_text_idempotent(s: str, case: str) -> None:
    rule = TextRule(target="x", case=case)  # type: ignore[arg-type]
    once, _ = clean_text(s, rule, TOK)
    if once is not None:
        again, kinds = clean_text(once, rule, TOK)
        assert again == once and not kinds


@settings(derandomize=True, database=None, max_examples=100, deadline=None)
@given(st.lists(st.text(max_size=20), min_size=1, max_size=5))
def test_clean_table_idempotent_and_deterministic(vals: list[str]) -> None:
    pol = CleaningPolicy(max_reject_rate=1.1)
    first = run(["r"], [[v] for v in vals], {"r": "region"}, policy=pol)
    again = run(["r"], [[r[0]] for r in first.rows], {"r": "region"}, policy=pol)  # type: ignore[misc]
    assert again.rows == first.rows and not again.corrections.counts
    assert run(["r"], [[v] for v in vals], {"r": "region"}, policy=pol).rows == first.rows


# --- configuration --------------------------------------------------------------------------


def test_bad_cleaning_config(tmp_path: Path) -> None:
    ok = {
        "fields": [
            {"target": t, "case": "keep"}
            for t in ("country_name", "country_code", "region", "indicator_code", "indicator_name")
        ]
    }
    parse_cleaning(ok, DICT)
    bad = [
        {"fields": ok["fields"][:-1]},  # incomplete policy
        {"fields": [*ok["fields"], {"target": "year"}]},  # non-string field
        {"fields": [*ok["fields"], {"target": "nope"}]},
        {"fields": [*ok["fields"], ok["fields"][0]]},  # duplicate
        {"fields": [{**ok["fields"][0], "case": "weird"}, *ok["fields"][1:]]},
        {"fields": [{**ok["fields"][0], "extra": 1}, *ok["fields"][1:]]},
        [],
    ]
    for b in bad:
        with pytest.raises(ConfigError):
            parse_cleaning(b, DICT)
    p = tmp_path / "c.yaml"
    p.write_text("fields: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_cleaning(p, DICT)
    p.write_text("fields: !!python/object/apply:os.system ['x']", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_cleaning(p, DICT)
    with pytest.raises(ConfigError):
        load_cleaning(tmp_path / "missing.yaml", DICT)


def test_mapping_errors() -> None:
    with pytest.raises(ConfigError):
        run(["c"], [["x"]], {"c": "unknown_field"})
    with pytest.raises(ConfigError):
        run(["c"], [["x"]], {"nope": "region"})


# --- mapping, unmapped columns, join keys ------------------------------------------------------


def test_unmapped_columns_ignored_and_listed() -> None:
    res = run(["region", "extra"], [["a", "z"]], {"region": "region"})
    assert res.columns == ["region"] and res.rows == [["A"]] and res.unmapped == ["extra"]


def test_join_key_only_when_policy_asks() -> None:
    cols, rows = ["r", "y"], [["Tanger-Tétouan-Al Hoceïma", "2020"]]
    plain = run(cols, rows, {"r": "region", "y": "year"})
    assert plain.columns == ["region", "year"]
    keyed = run(cols, rows, {"r": "region", "y": "year"}, policy=CleaningPolicy(join_keys=True))
    assert keyed.columns == ["region", "year", "region_key"]
    assert keyed.rows[0] == ["Tanger-Tétouan-Al Hoceïma", 2020, "tanger_tetouan_al_hoceima"]


# --- drift fixtures ----------------------------------------------------------------------------


def cleaned(version: str):
    table = read_file(DRIFT / version / "population.csv")
    cands = map_columns(table.columns, DICT)
    mapping = {c: m[0].target for c, m in cands.items() if m}
    return clean_table(table, mapping, dictionary=DICT, transforms=TRANSFORMS, text_rules=RULES,
                       source=f"drift_{version}", run_id="r", now=lambda: T0)  # fmt: skip


def test_v3_equals_v1_after_cleaning() -> None:
    v1, v3 = cleaned("v1"), cleaned("v3")
    assert v1.columns == v3.columns
    assert v1.rows == v3.rows and v1.rows
    assert v1.rows[0] == ["Tanger-Tétouan-Al Hoceïma", 2019, 1080551, 16.1]
    assert v3.corrections.counts[("population_total", "number_locale_parsed")] == len(v3.rows)
    assert not v1.corrections.counts
    assert cleaned("v2").rows  # smoke: v2 cleans too
