import random
from pathlib import Path

import pytest

from adp.connectors.files import RawTable, read_file
from adp.errors import SchemaError
from adp.schema.profile import profile_column, profile_table

DRIFT = Path(__file__).parent.parent / "data" / "drift"


def t(values: list[str | None]) -> str:
    return profile_column("c", values).inferred_type


def hint(values: list[str | None]) -> str | None:
    return profile_column("c", values).format_hint


@pytest.mark.parametrize(
    "values",
    [
        ["12,5", "3,25"],
        ["12.5", "3.25"],
        ["12,5", "12.5", "7"],
        ["1 080 551,5", "2 000,25"],
        ["1,234.56", "2,000.5"],
        ["1.234,56", "2.000,5"],
        ["16,1 %", "6,3 %"],
        ["-1.5", "+2.5", "1e3", "2.5E-2"],
    ],
)
def test_floats(values: list[str]) -> None:
    assert t(values) == "float"


@pytest.mark.parametrize(
    "values",
    [["1 080 551", "2 000"], ["1 080 551", "5"], ["1 080", "5"], ["-12", "7", "0"]],
)
def test_integers(values: list[str]) -> None:
    assert t(values) == "integer"


def test_number_hints_and_stats() -> None:
    p = profile_column("c", ["16,1 %", "6,3 %", "10 %"])
    assert p.inferred_type == "float"
    assert p.format_hint is not None
    assert "percent" in p.format_hint and "decimal_comma" in p.format_hint
    assert (p.min, p.max) == (6.3, 16.1)
    assert p.mean == pytest.approx((16.1 + 6.3 + 10) / 3)
    assert "thousands_space" in (hint(["1 080 551", "2 000"]) or "")
    assert hint(["12.5", "3.25"]) is None


def test_ambiguous_thousands() -> None:
    # '1,234' = EN thousands (integer); '1.234' = decimal dot (float); never crashes.
    assert t(["1,234", "5,678"]) == "integer"
    assert profile_column("c", ["1,234"]).max == 1234
    assert t(["1.234", "5.678"]) == "float"
    assert profile_column("c", ["1.234"]).max == 1.234
    assert t(["1.234.567"]) == "integer"
    assert t(["0,123", "0,5"]) == "float"


def test_leading_zero_codes_are_strings() -> None:
    assert t(["0612345678", "0712345678"]) == "string"
    assert t(["007", "042"]) == "string"


def test_dates() -> None:
    assert t(["2024-01-31", "2023-12-01"]) == "date"
    assert t(["31/01/2024", "01/12/2023"]) == "date"
    assert hint(["31/01/2024"]) == "%d/%m/%Y"
    assert t(["2024/01/31"]) == "date"
    assert hint(["2024/01/31"]) == "%Y/%m/%d"
    assert t(["2024-01-31T10:00:00", "2024-01-31 10:00", "2024-01-31T10:00:00Z"]) == "datetime"
    assert t(["2024-01-31", "2024-01-31T10:00:00"]) == "datetime"
    assert t(["2024-02-30"]) == "string"


def test_date_ambiguity() -> None:
    assert hint(["03/04/2024"]) == "%d/%m/%Y"  # default day-first
    assert hint(["03/04/2024", "12/25/2024"]) == "%m/%d/%Y"  # 25 cannot be a month
    assert hint(["03/04/2024", "25/12/2024"]) == "%d/%m/%Y"


def test_years_stay_integer() -> None:
    p = profile_column("annee", ["2019", "2020", "2021"])
    assert p.inferred_type == "integer"


def test_booleans() -> None:
    assert t(["true", "False", "TRUE"]) == "boolean"
    assert t(["oui", "non", "oui"]) == "boolean"
    assert t(["0", "1", "1", "0"]) == "boolean"
    assert t(["0", "1", "2"]) == "integer"
    assert t(["1", "1"]) == "integer"
    assert t(["1", "5", "10"]) == "integer"


def test_empty_and_nullable() -> None:
    p = profile_column("c", [None, "", "  "])
    assert p.inferred_type == "empty"
    assert p.null_count == 3 and p.row_count == 3 and p.nullable
    q = profile_column("c", ["1", None, "2"])
    assert q.nullable and q.null_count == 1 and q.row_count == 3
    assert not profile_column("c", ["1", "2"]).nullable
    assert profile_column("c", []).inferred_type == "empty"


def test_mixed_below_threshold_is_string() -> None:
    p = profile_column("c", ["1", "2", "3", "abc"])
    assert p.inferred_type == "string"
    assert p.type_confidence == 0.75 < 0.95
    # noise tolerated when the threshold is lowered
    q = profile_column("c", ["1", "2", "3", "abc"], min_type_confidence=0.7)
    assert q.inferred_type == "integer" and q.type_confidence == 0.75
    ok = profile_column("c", [str(i) for i in range(99)] + ["x"])
    assert ok.inferred_type == "integer" and ok.type_confidence == 0.99
    assert profile_column("c", ["a", "b"]).type_confidence == 1.0


def test_cardinality_and_string_lengths() -> None:
    p = profile_column("c", ["bb", "a", "bb", None, "cccc"])
    assert p.distinct_count == 3
    assert (p.min_length, p.max_length) == (1, 4)
    assert p.min is None and p.max is None and p.mean is None


def test_examples_deterministic_and_optional() -> None:
    vals: list[str | None] = ["b", "a", "b", "c", "d", "e", "f", "a", "b"]
    p = profile_column("c", vals)
    assert p.examples == ("b", "a", "c", "d", "e")
    shuffled = vals[:]
    random.Random(1).shuffle(shuffled)
    assert profile_column("c", shuffled).examples == p.examples
    assert profile_column("c", vals, max_examples=2).examples == ("b", "a")
    assert profile_column("c", vals, max_examples=0).examples == ()


def test_invalid_params() -> None:
    with pytest.raises(SchemaError):
        profile_column("c", ["1"], max_examples=-1)
    with pytest.raises(SchemaError):
        profile_column("c", ["1"], min_type_confidence=0)


def test_profile_is_frozen() -> None:
    p = profile_column("c", ["1"])
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        p.name = "x"  # type: ignore[misc]


def table(rows: list[list[str | None]], cols: list[str] | None = None) -> RawTable:
    return RawTable(columns=cols or ["id", "name", "d"], rows=rows)


ROWS: list[list[str | None]] = [
    ["1", "a", "2024-01-31"],
    ["2", "b", "2024-02-01"],
    ["3", None, "2024-02-02"],
]


def test_fingerprint_stable_and_value_independent() -> None:
    a = profile_table(table(ROWS))
    assert len(a.fingerprint) == 64
    assert a.fingerprint == profile_table(table(ROWS)).fingerprint
    shuffled = [ROWS[2], ROWS[0], ROWS[1]]
    assert profile_table(table(shuffled)).fingerprint == a.fingerprint
    other: list[list[str | None]] = [["10", "z", "2000-05-05"], ["11", None, "2001-05-05"]]
    assert profile_table(table(other)).fingerprint == a.fingerprint
    assert profile_table(table(ROWS), max_examples=0).fingerprint == a.fingerprint


def test_fingerprint_sensitive_to_structure() -> None:
    base = profile_table(table(ROWS)).fingerprint
    renamed = profile_table(table(ROWS, ["id", "nom", "d"])).fingerprint
    added = profile_table(table([r + ["x"] for r in ROWS], ["id", "name", "d", "e"])).fingerprint
    retyped = profile_table(table([["x", *r[1:]] for r in ROWS])).fingerprint
    nullable = profile_table(table([[r[0], "n", r[2]] for r in ROWS])).fingerprint
    assert len({base, renamed, added, retyped, nullable}) == 5


def test_column_order_insensitive_variant() -> None:
    a = profile_table(table(ROWS))
    swapped = RawTable(columns=["d", "id", "name"], rows=[[r[2], r[0], r[1]] for r in ROWS])
    b = profile_table(swapped)
    assert a.fingerprint != b.fingerprint
    assert a.fingerprint_unordered == b.fingerprint_unordered


def test_short_rows_are_padded() -> None:
    p = profile_table(RawTable(columns=["a", "b"], rows=[["1"], ["2", "x"]]))
    assert p.columns[1].null_count == 1 and p.columns[1].row_count == 2


def test_drift_fixtures() -> None:
    fps = {v: profile_table(read_file(DRIFT / v / "population.csv")) for v in ("v1", "v2", "v3")}
    assert len({f.fingerprint for f in fps.values()}) == 3
    v1 = {c.name: c for c in fps["v1"].columns}
    assert v1["annee"].inferred_type == "integer"
    v3 = {c.name: c for c in fps["v3"].columns}
    assert v3["population_totale"].inferred_type == "integer"
    assert v3["population_totale"].format_hint == "thousands_space"
    assert v3["taux_chomage"].inferred_type == "float"
    assert v3["taux_chomage"].format_hint == "decimal_comma,percent"
    assert "taux_activite" not in v3
