"""Rules mapping: cascade, fuzzy thresholds, false positives, determinism, noisy baseline."""

import csv
import socket
from pathlib import Path

import pytest
from pydantic import ValidationError

from adp.mapping.dictionary import CanonicalField, SemanticDictionary, load_dictionary
from adp.mapping.rules import RulesConfig, map_column, map_columns
from adp.schema.profile import ColumnProfile

ROOT = Path(__file__).resolve().parents[2]
DICT = load_dictionary(ROOT / "config" / "semantic" / "canonical.yaml")


def _top(col: str, cfg: RulesConfig | None = None):
    r = map_column(col, DICT, config=cfg or RulesConfig())
    return r[0] if r else None


# --- backlog validation cases, transposed to the production domain -----------------------
def test_normalized_exotic_punctuation_accents_case() -> None:
    for col in ["Unemployment Rate (%)", "LIFE-EXPECTANCY", "GDP.Per.Capita.USD", "Région"]:
        c = _top(col)
        assert c is not None and c.method == "normalized" and c.score == 0.98, col
    assert _top("Unemployment Rate (%)").target == "unemployment_rate"  # type: ignore[union-attr]
    assert _top("Région").target == "region"  # type: ignore[union-attr]


def test_synonym_with_accents() -> None:
    c = _top("Taux de Chômage (%)")
    assert c and (c.target, c.method, c.score) == ("unemployment_rate", "synonym", 0.95)


@pytest.mark.parametrize(
    ("col", "target"),
    [("unempl_rt", "unemployment_rate"), ("pop_tot", "population_total"),
     ("gdp_per_cap", "gdp_per_capita_usd")],
)  # fmt: skip
def test_fuzzy_abbreviations_above_threshold(col: str, target: str) -> None:
    c = _top(col)
    assert c and c.target == target and c.method == "fuzzy"
    assert c.score >= RulesConfig().min_score
    assert c.score < RulesConfig().synonym_score


# --- literal backlog cases with an in-memory mini dictionary ----------------------------
MINI = SemanticDictionary(
    fields=(
        CanonicalField(name="customer_id", type="string", description="id", synonyms=("n_client",)),
        CanonicalField(name="customer_name", type="string", description="nom"),
    )
)


def test_literal_backlog_cases_mini_dictionary() -> None:
    # 'N° Client' folds to n_client: reachable as a synonym (never as a name) -> "synonym".
    c = map_column("N° Client", MINI)[0]
    assert (c.target, c.method) == ("customer_id", "synonym")
    assert map_column("Customer ID", MINI)[0].method == "normalized"
    c = map_column("cust_nm", MINI)[0]
    assert (c.target, c.method) == ("customer_name", "fuzzy")
    assert c.score >= RulesConfig().min_score


# --- priority, thresholds, config ---------------------------------------------------------
def test_priority_exact_normalized_synonym_fuzzy() -> None:
    assert _top("year").method == "exact"  # type: ignore[union-attr]
    assert _top("Year").method == "normalized"  # type: ignore[union-attr]
    assert _top("annee").method == "synonym"  # type: ignore[union-attr]
    assert _top("anee").method == "fuzzy"  # type: ignore[union-attr]
    scores = [_top(c).score for c in ("year", "Year", "annee", "anee")]  # type: ignore[union-attr]
    assert scores == sorted(scores, reverse=True)


def test_threshold_configurable() -> None:
    assert _top("unempl_rt") is not None
    assert _top("unempl_rt", RulesConfig(min_score=0.8)) is None


def test_top_k_and_one_candidate_per_field() -> None:
    r = map_column("gdp_usd_courants", DICT, config=RulesConfig(top_k=1))
    assert len(r) == 1
    r = map_column("pays_nom", DICT, config=RulesConfig(min_score=0.3, top_k=50))
    targets = [c.target for c in r]
    assert len(targets) == len(set(targets))


def test_invalid_config_rejected() -> None:
    for kw in ({"min_score": 1.5}, {"top_k": 0}, {"fuzzy_weight": 0}, {"synonym_score": 0.99}):
        with pytest.raises(ValidationError):
            RulesConfig(**kw)


@pytest.mark.parametrize("col", ["", "   ", "()", None])
def test_empty_inputs(col: str | None) -> None:
    assert map_column(col, DICT) == []


def test_deterministic_and_sorted() -> None:
    cols = ["pays_nom", "gdp_usd_courants", "cntry", "taux_chomage"]
    a = map_columns(cols, DICT)
    assert a == map_columns(cols, DICT)
    for cands in a.values():
        keys = [(-c.score, c.target) for c in cands]
        assert keys == sorted(keys)


def test_tie_flag_deterministic() -> None:
    d = SemanticDictionary(
        fields=(
            CanonicalField(name="aaa_x", type="string", description="d"),
            CanonicalField(name="bbb_x", type="string", description="d"),
        )
    )
    r = map_column("ccc_x", d, config=RulesConfig(min_score=0.3))
    assert [c.target for c in r] == ["aaa_x", "bbb_x"] and r[0].tie and not r[1].tie


def test_type_compatibility_uses_profile_only() -> None:
    def prof(t: str) -> ColumnProfile:
        return ColumnProfile(
            name="x",
            inferred_type=t,  # type: ignore[arg-type]
            type_confidence=1.0,
            nullable=False,
            null_count=0,
            row_count=5,
            distinct_count=5,
        )

    assert map_column("taux_chomage", DICT)[0].type_compatible is None
    for t, ok in [("float", True), ("integer", True), ("string", False)]:
        c = map_column("taux_chomage", DICT, profile=prof(t))[0]
        assert c.type_compatible is ok and c.score == 0.95


def test_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise AssertionError("network call")

    monkeypatch.setattr(socket, "socket", boom)
    assert _top("unempl_rt") is not None


# --- fixtures and World Bank schema -------------------------------------------------------
def _header(p: str) -> list[str]:
    with (ROOT / "tests/data/drift" / p / "population.csv").open(encoding="utf-8") as f:
        return next(csv.reader(f))


def test_drift_fixture_columns() -> None:
    expected = {
        "region": "region", "annee": "year", "population": "population_total",
        "taux_chomage": "unemployment_rate", "population_totale": "population_total",
        "taux_activite": "activity_rate",
    }  # fmt: skip
    for v in ("v1", "v2", "v3"):
        for col in _header(v):
            c = _top(col)
            assert c and c.target == expected[col] and c.method in ("exact", "synonym")


def test_world_bank_columns() -> None:
    expected = {
        "countryiso3code": "country_code", "date": "period", "indicator.id": "indicator_code",
        "country.value": "country_name", "indicator.value": "indicator_name",
    }  # fmt: skip
    for col, target in expected.items():
        c = _top(col)
        assert c and c.target == target and c.score >= 0.95, col
    assert _top("value") is None  # generic token alone: no strong match, by design


# --- false positives ----------------------------------------------------------------------
NEGATIVES = [
    "email", "commentaire", "id_commande", "date_naissance", "telephone", "adresse",
    "prix_unitaire", "nom_produit", "statut", "quantite", "nb", "total", "taux",
]  # fmt: skip


def test_negatives_below_review_threshold() -> None:
    worst = 0.0
    for col in NEGATIVES:
        r = map_column(col, DICT, config=RulesConfig(min_score=0.0, top_k=1))
        best = r[0].score if r else 0.0
        print(f"NEG {col:16} best={best:.3f} {r[0].target if r else '-'}")  # noqa: T201
        worst = max(worst, best)
        assert best < 0.90, col
    # documented measure: no negative reaches the review band by a wide margin
    assert worst < 0.75


# --- noisy baseline (reference for P3-09) -------------------------------------------------
NOISY = {
    "unempl_rt": "unemployment_rate", "unemploymnt_rate": "unemployment_rate",
    "chomage_taux": "unemployment_rate", "tx_chom": "unemployment_rate",
    "Taux Chomage": "unemployment_rate", "pop_tot": "population_total",
    "POPULATION TOTALE": "population_total", "popultion": "population_total",
    "nb habitants": "population_total", "gdp_per_cap": "gdp_per_capita_usd",
    "PIB par hab": "gdp_per_capita_usd", "gdp_usd_courants": "gdp_current_usd",
    "Life Expct": "life_expectancy", "esperance vie": "life_expectancy",
    "inflation_pct": "inflation_rate", "Taux d'inflation (%)": "inflation_rate",
    "literacy_pct": "literacy_rate", "taux alphabet.": "literacy_rate",
    "taux_activ": "activity_rate", "Labor force particip": "activity_rate",
    "pays_nom": "country_name", "cntry": "country_name", "iso": "country_code",
    "code pays iso": "country_code", "Région admin": "region", "yr": "year",
    "Année de référence": "year", "obs_date": "period", "indic_code": "indicator_code",
    "libellé indicateur": "indicator_name",
}  # fmt: skip


def test_noisy_baseline_top1_rate() -> None:
    misses, rows = [], []
    for col, want in NOISY.items():
        r = map_column(col, DICT)
        got = (r[0].target, r[0].score, r[0].method) if r else (None, 0.0, "-")
        rows.append(f"{col:22} want={want:20} got={got}")
        if got[0] != want:
            misses.append(col)
    rate = 1 - len(misses) / len(NOISY)
    print("\n".join(rows))  # noqa: T201
    print(f"TOP1 {rate:.0%} ({len(NOISY) - len(misses)}/{len(NOISY)}) misses={misses}")  # noqa: T201
    assert rate >= 0.7
