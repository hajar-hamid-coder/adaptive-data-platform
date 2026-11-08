"""Schema diff: add/drop/type/format/nullability/reorder and probable renames (P2-03).

The 20 negative control cases (`NEGATIVE_CASES`) and the positive ones (`POSITIVE_CASES`) were
written BEFORE the scoring weights were chosen, from common real-world drifts, and are never
tuned away: if a negative case fails, the algorithm is fixed, not the case.
Data is synthetic and generated with fixed seeds (no real data).
"""

import json
import random
from collections.abc import Callable, Iterator
from datetime import date, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, create_engine

from adp.connectors.files import RawTable, read_file
from adp.errors import SchemaError
from adp.schema.diff import ChangeKind, DiffConfig, SchemaDiff, diff_schemas
from adp.schema.profile import ColumnProfile, SchemaFingerprint, profile_table
from adp.schema.registry import SchemaChange, SchemaRegistry, metadata

DRIFT = Path(__file__).parent.parent / "data" / "drift"
N = 50

Col = Callable[[int], list[str | None]]  # seed -> N raw values


def drift(v: str) -> SchemaFingerprint:
    return profile_table(read_file(DRIFT / v / "population.csv"))


def expected(v: str) -> list[dict[str, str]]:
    data = json.loads((DRIFT / v / "expected_changes.json").read_text(encoding="utf-8"))
    return list(data["changes_from_previous"])


# --- synthetic column generators (deterministic) ---------------------------------------------


def ints(lo: int, hi: int) -> Col:
    return lambda s: [str(random.Random(s).randint(lo, hi)) for _ in range(N)]


def seq(start: int) -> Col:
    return lambda s: [str(start + i) for i in range(N)]


def floats(lo: float, hi: float, nd: int = 2) -> Col:
    def gen(s: int) -> list[str | None]:
        r = random.Random(s)
        return [f"{r.uniform(lo, hi):.{nd}f}" for _ in range(N)]

    return gen


def pick(*choices: str) -> Col:
    def gen(s: int) -> list[str | None]:
        r = random.Random(s)
        return [r.choice(choices) for _ in range(N)]

    return gen


def dates(y0: int, y1: int) -> Col:
    def gen(s: int) -> list[str | None]:
        r = random.Random(s)
        span = (date(y1, 12, 31) - date(y0, 1, 1)).days
        return [(date(y0, 1, 1) + timedelta(days=r.randint(0, span))).isoformat() for _ in range(N)]

    return gen


def words(lo: int, hi: int, prefix: str = "") -> Col:
    def gen(s: int) -> list[str | None]:
        r = random.Random(s)
        alpha = "abcdefghijklmnopqrstuvwxyz"
        return [
            prefix + "".join(r.choice(alpha) for _ in range(r.randint(lo, hi))) for _ in range(N)
        ]

    return gen


def with_nulls(col: Col, every: int) -> Col:
    return lambda s: [None if i % every == 0 else v for i, v in enumerate(col(s))]


def table(cols: dict[str, Col], seed: int) -> SchemaFingerprint:
    """Each column gets its own seed derived from its name, so a column carried over unchanged
    (same name, same generator) has the same values in the old and new table."""
    names = list(cols)
    values = [cols[n](seed + sum(map(ord, n))) for n in names]
    rows: list[list[str | None]] = [list(r) for r in zip(*values, strict=True)]
    return profile_table(RawTable(columns=names, rows=rows))


ID = seq(1)
EMAIL = words(5, 12, "u_")
VILLE = pick("Rabat", "Casablanca", "Fes", "Tanger", "Agadir", "Oujda", "Meknes")

# (label, old columns, new columns) - NO rename exists in any of these pairs.
NEGATIVE_CASES: list[tuple[str, dict[str, Col], dict[str, Col]]] = [
    (
        "identical",
        {"id": ID, "email": EMAIL, "age": ints(18, 90)},
        {"id": ID, "email": EMAIL, "age": ints(18, 90)},
    ),
    (
        "reordered",
        {"id": ID, "email": EMAIL, "age": ints(18, 90)},
        {"age": ints(18, 90), "id": ID, "email": EMAIL},
    ),
    ("nullability_only", {"id": ID, "email": EMAIL}, {"id": ID, "email": with_nulls(EMAIL, 5)}),
    (
        "email_vs_montant_total",
        {"id": ID, "email": EMAIL},
        {"id": ID, "montant_total": floats(10, 5000)},
    ),
    (
        "ville_vs_statut_commande",
        {"id": ID, "ville": VILLE},
        {"id": ID, "statut_commande": pick("en_attente", "expediee", "livree", "annulee")},
    ),
    ("age_vs_quantite", {"id": ID, "age": ints(18, 90)}, {"id": ID, "quantite": ints(1, 20)}),
    (
        "id_client_vs_id_commande",
        {"ligne": ID, "id_client": ints(1, 300)},
        {"ligne": ID, "id_commande": seq(100000)},
    ),
    (
        "date_naissance_vs_date_commande",
        {"id": ID, "date_naissance": dates(1950, 2005)},
        {"id": ID, "date_commande": dates(2023, 2024)},
    ),
    (
        "taux_chomage_vs_taux_activite",
        {"region": VILLE, "taux_chomage": floats(6, 20, 1)},
        {"region": VILLE, "taux_activite": floats(38, 55, 1)},
    ),
    (
        "prix_ht_vs_prix_ttc",
        {"id": ID, "prix_ht": floats(10, 1000)},
        {"id": ID, "prix_ttc": floats(12, 1200)},
    ),
    (
        "code_postal_vs_code_produit",
        {"id": ID, "code_postal": pick("10000", "20250", "30000")},
        {"id": ID, "code_produit": words(8, 10, "SKU")},
    ),
    ("nom_vs_prenom", {"id": ID, "nom": words(4, 10)}, {"id": ID, "prenom": words(4, 10)}),
    (
        "date_debut_vs_date_fin",
        {"id": ID, "date_debut": dates(2020, 2021)},
        {"id": ID, "date_fin": dates(2022, 2023)},
    ),
    (
        "ville_vs_pays",
        {"id": ID, "ville": VILLE},
        {"id": ID, "pays": pick("Maroc", "France", "Espagne", "Senegal", "Tunisie")},
    ),
    (
        "three_dropped_three_added",
        {"id": ID, "telephone": words(10, 10), "adresse": words(15, 30), "sexe": pick("F", "M")},
        {
            "id": ID,
            "canal": pick("web", "magasin", "app"),
            "remise": floats(0, 30),
            "poids": floats(0.1, 25),
        },
    ),
    (
        "same_name_type_change",
        {"id": ID, "age": ints(18, 90)},
        {"id": ID, "age": pick("jeune", "adulte", "senior")},
    ),
    (
        "latitude_vs_longitude",
        {"id": ID, "latitude": floats(27, 36, 5)},
        {"id": ID, "longitude": floats(-13, -1, 5)},
    ),
    (
        "montant_ht_vs_montant_tva",
        {"id": ID, "montant_ht": floats(10, 1000)},
        {"id": ID, "montant_tva": floats(2, 200)},
    ),
    (
        "whole_new_schema",
        {"produit": words(5, 15), "categorie": pick("a", "b", "c"), "stock": ints(0, 500)},
        {"temperature": floats(-5, 45, 1), "humidite": ints(10, 100), "vent": floats(0, 120, 1)},
    ),
    (
        "is_actif_vs_is_premium",
        {"id": ID, "is_actif": pick("oui", "non")},
        {"id": ID, "is_premium": pick("true", "false")},
    ),
]

# (label, old columns, new columns, expected rename old -> new): real renames, same values.
POSITIVE_CASES: list[tuple[str, dict[str, Col], dict[str, Col], tuple[str, str]]] = [
    ("camel_to_snake", {"id": ID, "codePostal": pick("10000", "20250")},
     {"id": ID, "code_postal": pick("10000", "20250")}, ("codePostal", "code_postal")),
    ("initials_cp", {"id": ID, "cp": pick("10000", "20250")},
     {"id": ID, "code_postal": pick("10000", "20250")}, ("cp", "code_postal")),
    ("prefix_tel", {"id": ID, "tel": words(10, 10)},
     {"id": ID, "telephone": words(10, 10)}, ("tel", "telephone")),
    ("accent_case", {"id": ID, "Prénom": words(4, 10)},
     {"id": ID, "prenom": words(4, 10)}, ("Prénom", "prenom")),
    ("suffix_nom_client", {"id": ID, "nom": words(4, 10)},
     {"id": ID, "nom_client": words(4, 10)}, ("nom", "nom_client")),
    ("abbrev_qte", {"id": ID, "qte": ints(1, 20)},
     {"id": ID, "quantite": ints(1, 20)}, ("qte", "quantite")),
    ("camel_date", {"id": ID, "dateNaissance": dates(1950, 2005)},
     {"id": ID, "date_naissance": dates(1950, 2005)}, ("dateNaissance", "date_naissance")),
    ("prefix_dropped", {"id": ID, "adresse_email": EMAIL},
     {"id": ID, "email": EMAIL}, ("adresse_email", "email")),
]  # fmt: skip


def _same_values(
    old: dict[str, Col], new: dict[str, Col], seed: int
) -> tuple[SchemaFingerprint, SchemaFingerprint]:
    """Positive cases: the renamed column keeps exactly its values."""
    a = table(old, seed)
    rows_src = {n: old[n](seed + sum(map(ord, n))) for n in old}
    renamed = [n for n in new if n not in old]
    gone = [n for n in old if n not in new]
    cols = {n: (rows_src[gone[0]] if n in renamed else new[n](seed + sum(map(ord, n))))
            for n in new}  # fmt: skip
    rows: list[list[str | None]] = [list(r) for r in zip(*cols.values(), strict=True)]
    return a, profile_table(RawTable(columns=list(cols), rows=rows))


# --- backlog validation test: simulator v1 -> v2 -> v3 ---------------------------------------


def _ops(d: SchemaDiff) -> set[tuple[str, ...]]:
    out: set[tuple[str, ...]] = set()
    for c in d.changes:
        if c.kind is ChangeKind.RENAMED:
            out.add((c.kind.value, c.old_column or "", c.column or ""))
        elif c.kind is not ChangeKind.REORDERED:
            out.add((c.kind.value, c.column or ""))
    return out


def test_v1_to_v2_rename_and_add() -> None:
    d = diff_schemas(drift("v1"), drift("v2"))
    cfg = DiffConfig()
    [ren] = d.renames
    assert (ren.old_column, ren.new_column) == ("population", "population_totale")
    assert ren.score >= cfg.review_threshold
    assert ren.decision == "auto" and ren.score >= cfg.auto_threshold
    want = set()
    for e in expected("v2"):
        if e["op"] == "rename_column":
            want.add(("rename_column", e["from"], e["to"]))
        else:
            want.add((e["op"], e["column"]))
    assert _ops(d) == want


def test_v2_to_v3_drop_and_format_changes_no_false_rename() -> None:
    """The simulator writes integers/floats as formatted text in v3 (`1 080 551`, `16,1 %`).
    The profiler normalises them back to integer/float, so what changes structurally is the
    FORMAT (format_hint), reported as `format_change`, distinct from a real `type_change`."""
    d = diff_schemas(drift("v2"), drift("v3"))
    assert d.renames == ()
    want = set()
    for e in expected("v3"):
        op = "format_change" if e["op"] == "type_change" else e["op"]
        want.add((op, e["column"]))
    assert _ops(d) == want
    fmt = {c.column: c.detail for c in d.changes if c.kind is ChangeKind.FORMAT_CHANGED}
    assert fmt["population_totale"] == {"type": "integer", "from": None, "to": "thousands_space"}
    assert fmt["taux_chomage"]["to"] == "decimal_comma,percent"


def test_v1_to_v3_consistent() -> None:
    d = diff_schemas(drift("v1"), drift("v3"))
    assert _ops(d) == {
        ("rename_column", "population", "population_totale"),
        ("format_change", "population_totale"),
        ("format_change", "taux_chomage"),
    }
    assert d.renames[0].decision == "auto"


# --- control cases ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "old", "new"), NEGATIVE_CASES, ids=[c[0] for c in NEGATIVE_CASES]
)
def test_negative_control_no_rename(label: str, old: dict[str, Col], new: dict[str, Col]) -> None:
    d = diff_schemas(table(old, 7), table(new, 7))
    assert d.renames == (), f"{label}: false rename {d.renames}"
    assert not any(c.kind is ChangeKind.RENAMED for c in d.changes)


def test_twenty_negative_controls() -> None:
    assert len(NEGATIVE_CASES) == 20
    assert len({c[0] for c in NEGATIVE_CASES}) == 20


@pytest.mark.parametrize(
    ("label", "old", "new", "pair"), POSITIVE_CASES, ids=[c[0] for c in POSITIVE_CASES]
)
def test_positive_control_rename_found(
    label: str, old: dict[str, Col], new: dict[str, Col], pair: tuple[str, str]
) -> None:
    a, b = _same_values(old, new, 11)
    d = diff_schemas(a, b)
    assert [(r.old_column, r.new_column) for r in d.renames] == [pair], label


def test_negative_controls_expected_kinds() -> None:
    by = {c[0]: diff_schemas(table(c[1], 7), table(c[2], 7)) for c in NEGATIVE_CASES}
    assert by["identical"].changes == ()
    assert [c.kind for c in by["reordered"].changes] == [ChangeKind.REORDERED]
    assert [(c.kind, c.column) for c in by["nullability_only"].changes] == [
        (ChangeKind.NULLABILITY_CHANGED, "email")
    ]
    assert [(c.kind, c.detail["to"]) for c in by["same_name_type_change"].changes] == [
        (ChangeKind.TYPE_CHANGED, "string")
    ]
    kinds = [c.kind for c in by["email_vs_montant_total"].changes]
    assert kinds == [ChangeKind.ADDED, ChangeKind.REMOVED]


# --- algorithm properties ---------------------------------------------------------------------


def test_deterministic_and_input_order_independent() -> None:
    old, new = drift("v1"), drift("v2")
    d1 = diff_schemas(old, new)
    assert d1 == diff_schemas(old, new)
    d2 = diff_schemas(list(reversed(old.columns)), list(reversed(new.columns)))
    assert d1.renames == d2.renames and d1.candidates == d2.candidates
    no_reorder = [c for c in d1.changes if c.kind is not ChangeKind.REORDERED]
    assert no_reorder == [c for c in d2.changes if c.kind is not ChangeKind.REORDERED]


def test_one_to_one_matching() -> None:
    """Two dropped columns compete for one added target: only the best one gets it."""
    old = table({"id": ID, "code_postal": pick("10000", "20250"), "cp": pick("10000", "20250")}, 3)
    new = table({"id": ID, "codePostal": pick("10000", "20250")}, 3)
    d = diff_schemas(old, new)
    assert [(r.old_column, r.new_column) for r in d.renames] == [("code_postal", "codePostal")]
    assert (ChangeKind.REMOVED, "cp") in {(c.kind, c.column) for c in d.changes}
    assert len({r.new_column for r in d.renames}) == len(d.renames)


def test_thresholds_configurable() -> None:
    old, new = drift("v1"), drift("v2")
    score = diff_schemas(old, new).renames[0].score
    assert diff_schemas(old, new).renames[0].decision == "auto"
    review = DiffConfig(review_threshold=0.5, auto_threshold=min(1.0, score + 0.01))
    assert diff_schemas(old, new, config=review).renames[0].decision == "review"
    rejected = DiffConfig(review_threshold=min(1.0, score + 0.01), auto_threshold=1.0)
    d = diff_schemas(old, new, config=rejected)
    assert d.renames == ()
    assert {(c.kind, c.column) for c in d.changes} >= {
        (ChangeKind.REMOVED, "population"),
        (ChangeKind.ADDED, "population_totale"),
    }
    assert d.candidates[0].decision == "rejected"


def test_score_components_explained() -> None:
    r = diff_schemas(drift("v1"), drift("v2")).renames[0]
    for v in (r.score, r.name_sim, r.type_sim, r.dist_sim):
        assert 0.0 <= v <= 1.0
    assert r.type_sim == 1.0 and r.dist_sim == 1.0 and r.selected


@pytest.mark.parametrize(
    "kwargs",
    [
        {"review_threshold": 0.95, "auto_threshold": 0.9},
        {"review_threshold": 0.0},
        {"auto_threshold": 1.5},
        {"name_weight": -0.1},
        {"name_weight": 0, "type_weight": 0, "dist_weight": 0},
        {"generic_token_weight": 2},
        {"unknown": 1},
    ],
)
def test_invalid_config_rejected(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        DiffConfig(**kwargs)  # type: ignore[arg-type]


def test_config_frozen() -> None:
    with pytest.raises(ValidationError):
        DiffConfig().review_threshold = 0.1  # type: ignore[misc]


def test_empty_schemas() -> None:
    assert diff_schemas([], []).changes == ()
    one = drift("v1")
    assert {c.kind for c in diff_schemas([], one).changes} == {ChangeKind.ADDED}
    assert {c.kind for c in diff_schemas(one, []).changes} == {ChangeKind.REMOVED}


def test_duplicate_column_rejected() -> None:
    c = drift("v1").columns[0]
    with pytest.raises(SchemaError):
        diff_schemas([c, c], [])


def test_no_real_values_in_output() -> None:
    """Examples (real values) never leak into the diff nor the stored change rows."""
    old, new = drift("v1"), drift("v3")
    leaked = {e for c in [*old.columns, *new.columns] for e in c.examples}
    blob = json.dumps([ch.model_dump() for ch in diff_schemas(old, new).to_schema_changes()])
    assert leaked and not any(e in blob for e in leaked)


def test_column_profile_list_input() -> None:
    cols: list[ColumnProfile] = list(drift("v1").columns)
    assert diff_schemas(cols, drift("v2")) == diff_schemas(drift("v1"), drift("v2"))


# --- record_changes compatibility --------------------------------------------------------------


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = create_engine(f"sqlite:///{tmp_path / 'meta.db'}")
    metadata.create_all(eng)
    yield eng
    eng.dispose()


def test_record_changes_round_trip(engine: Engine) -> None:
    reg = SchemaRegistry(engine)
    fps = {v: drift(v) for v in ("v1", "v2", "v3")}
    vers = {v: reg.register("hcp_population", f) for v, f in fps.items()}
    for a, b in (("v1", "v2"), ("v2", "v3")):
        rows = diff_schemas(fps[a], fps[b]).to_schema_changes()
        n = reg.record_changes("hcp_population", vers[a].version, vers[b].version, rows)
        assert n == len(rows)
        reg.record_changes("hcp_population", vers[a].version, vers[b].version, rows)  # replay
        stored = [r.change for r in reg.changes("hcp_population", vers[b].version)]
        assert stored == rows
    v2 = {(c.change_type, c.column_name) for c in rows_of(reg, vers["v2"].version)}
    assert ("rename_column", "population_totale") in v2 and ("add_column", "taux_activite") in v2
    ren = next(c for c in rows_of(reg, vers["v2"].version) if c.change_type == "rename_column")
    assert ren.detail["from"] == "population" and ren.detail["decision"] == "auto"
    assert set(ren.detail["components"]) == {"name_sim", "type_sim", "dist_sim"}
    assert {k.value for k in ChangeKind} >= {c.change_type for c in rows_of(reg, 3)}


def rows_of(reg: SchemaRegistry, version: int) -> list[SchemaChange]:
    return [r.change for r in reg.changes("hcp_population", version)]
