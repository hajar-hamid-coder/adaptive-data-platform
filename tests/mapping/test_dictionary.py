"""Semantic dictionary: loading, normalization, duplicate detection, fixture coverage."""

import csv
from pathlib import Path

import pytest
import yaml

from adp.errors import ConfigError
from adp.mapping.dictionary import (
    CanonicalField,
    SemanticDictionary,
    load_dictionary,
    normalize,
)

ROOT = Path(__file__).resolve().parents[2]
CANONICAL = ROOT / "config" / "semantic" / "canonical.yaml"


@pytest.fixture(scope="module")
def dico() -> SemanticDictionary:
    return load_dictionary(CANONICAL)


def _field(name: str, *synonyms: str, type: str = "string") -> dict[str, object]:
    return {"name": name, "type": type, "description": "d", "synonyms": list(synonyms)}


def _write(tmp_path: Path, doc: object) -> Path:
    p = tmp_path / "dict.yaml"
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return p


def test_real_dictionary_loads_and_is_complete(dico: SemanticDictionary) -> None:
    assert 10 <= len(dico.fields) <= 20
    for f in dico.fields:
        assert f.description.strip()
        assert len(f.synonyms) >= 2, f.name


@pytest.mark.parametrize(
    ("term", "expected"),
    [
        ("POPULATION", "population_total"),
        ("Chômage", "unemployment_rate"),
        ("taux_de_chomage", "unemployment_rate"),
        ("Taux de chômage", "unemployment_rate"),
        ("tauxDeChomage", "unemployment_rate"),
        ("Région", "region"),
        ("annee", "year"),
        ("Année", "year"),
        ("PIB", "gdp_current_usd"),
        ("NY.GDP.MKTP.CD", "gdp_current_usd"),
        ("total-population", "population_total"),
        ("  Nb Habitants ", "population_total"),
        ("countryiso3code", "country_code"),
        ("unemployment_rate", "unemployment_rate"),  # field name is an implicit synonym
    ],
)
def test_lookup_is_case_accent_and_separator_insensitive(
    dico: SemanticDictionary, term: str, expected: str
) -> None:
    found = dico.lookup(term)
    assert found is not None and found.name == expected


def test_lookup_unknown_is_none(dico: SemanticDictionary) -> None:
    assert dico.lookup("zzz_inconnu") is None
    assert dico.lookup("") is None


def test_normalize_forms() -> None:
    assert normalize("Taux de chômage") == normalize("taux_de_chomage") == "taux_de_chomage"
    assert normalize("tauxDeChomage") == "taux_de_chomage"
    assert normalize("NY_GDP.MKTP/CD") == "ny_gdp_mktp_cd"
    assert normalize("Tanger-Tétouan") == "tanger_tetouan"
    assert normalize("  --  ") == ""


def test_candidates_by_token(dico: SemanticDictionary) -> None:
    names = {f.name for f in dico.candidates("taux chomage urbain")}
    assert "unemployment_rate" in names
    assert dico.candidates("zzz") == []


def test_duplicate_synonym_across_fields_is_rejected() -> None:
    with pytest.raises(ValueError, match=r"'chomage'.*'a'.*'b'"):
        SemanticDictionary(
            fields=(
                CanonicalField(name="a", type="float", description="d", synonyms=("Chômage",)),
                CanonicalField(name="b", type="float", description="d", synonyms=("chomage",)),
            )
        )


def test_field_name_colliding_with_synonym_is_rejected() -> None:
    with pytest.raises(ValueError, match="'pop'"):
        SemanticDictionary(
            fields=(
                CanonicalField(name="pop", type="integer", description="d"),
                CanonicalField(name="x", type="integer", description="d", synonyms=("Pop",)),
            )
        )


def test_duplicate_synonym_inside_one_field_is_folded() -> None:
    f = CanonicalField(name="a", type="string", description="d", synonyms=("Pays", "pays", "a"))
    d = SemanticDictionary(fields=(f,))
    found = d.lookup("PAYS")
    assert found is not None and found.name == "a"


def test_empty_synonym_rejected() -> None:
    with pytest.raises(ValueError, match="empty synonym"):
        CanonicalField(name="a", type="string", description="d", synonyms=("  - ",))


@pytest.mark.parametrize(
    "doc",
    [
        {"fields": [{**_field("a", "x"), "bogus": 1}]},  # unknown key
        {"fields": [_field("a", "x", type="decimal")]},  # unknown type
        {"fields": [_field("a", "x"), _field("a", "y")]},  # duplicate name
        {"fields": [_field("a", "x"), _field("b", "X")]},  # duplicate synonym
        {"fields": [_field("a", "")]},  # empty synonym
        {"fields": []},  # empty
        {"fields": [_field("Bad Name", "x")]},  # name not snake_case
        {"champs": []},  # unknown top-level key
    ],
)
def test_invalid_yaml_raises_config_error_with_file(tmp_path: Path, doc: object) -> None:
    path = _write(tmp_path, doc)
    with pytest.raises(ConfigError, match="dict.yaml"):
        load_dictionary(path)


def test_non_mapping_and_missing_file(tmp_path: Path) -> None:
    p = tmp_path / "list.yaml"
    p.write_text("- 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        load_dictionary(p)
    with pytest.raises(ConfigError, match="nope.yaml"):
        load_dictionary(tmp_path / "nope.yaml")


def test_loader_uses_safe_load(tmp_path: Path) -> None:
    p = tmp_path / "evil.yaml"
    p.write_text("fields: !!python/object/apply:os.getcwd []\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="evil.yaml"):
        load_dictionary(p)


# Columns deliberately without a canonical match: `value` alone is ambiguous (World Bank long
# format: its meaning depends on `indicator`), P3-02 must resolve it from context.
EXPECTED_UNMAPPED = {"value"}


def _csv_header(version: str) -> list[str]:
    path = ROOT / "tests" / "data" / "drift" / version / "population.csv"
    with path.open(encoding="utf-8", newline="") as fh:
        return next(csv.reader(fh))


@pytest.mark.parametrize(
    "columns",
    [
        _csv_header("v1"),
        _csv_header("v2"),
        _csv_header("v3"),
        ["countryiso3code", "date", "value", "indicator.id", "indicator.value", "country.value"],
    ],
    ids=["hcp_v1", "hcp_v2", "hcp_v3", "world_bank"],
)
def test_fixture_columns_are_covered(dico: SemanticDictionary, columns: list[str]) -> None:
    missing = {c for c in columns if dico.lookup(c) is None}
    assert missing <= EXPECTED_UNMAPPED, missing


def test_fixture_columns_map_to_expected_fields(dico: SemanticDictionary) -> None:
    expected = {
        "region": "region",
        "annee": "year",
        "population": "population_total",
        "population_totale": "population_total",
        "taux_chomage": "unemployment_rate",
        "taux_activite": "activity_rate",
        "countryiso3code": "country_code",
        "date": "period",
    }
    for col, name in expected.items():
        found = dico.lookup(col)
        assert found is not None and found.name == name, col
