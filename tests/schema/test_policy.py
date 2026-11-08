"""Impact classification and evolution policy (P2-04). Synthetic data only."""

from pathlib import Path
from typing import Any

import pytest

from adp.connectors.files import RawTable, read_file
from adp.errors import ConfigError
from adp.schema.diff import ChangeKind, ColumnChange, SchemaDiff, diff_schemas
from adp.schema.policy import (
    ChangeImpact,
    EvolutionAction,
    EvolutionPolicy,
    classify_change,
    classify_diff,
    load_policy,
    type_shift,
    with_impact,
)
from adp.schema.profile import SchemaFingerprint, profile_table

ROOT = Path(__file__).parent.parent.parent
POLICY_FILE = ROOT / "config" / "evolution_policy.yaml"
DRIFT = ROOT / "tests" / "data" / "drift"
PK = ("region", "annee")


@pytest.fixture(scope="module")
def policy() -> EvolutionPolicy:
    return load_policy(POLICY_FILE)


def ch(kind: ChangeKind, column: str | None, old: str | None = None, **detail: Any) -> ColumnChange:
    return ColumnChange(kind=kind, column=column, old_column=old, detail=detail)


def fp(columns: list[str], rows: list[list[str | None]]) -> SchemaFingerprint:
    return profile_table(RawTable(columns=columns, rows=rows))


def drift(v: str) -> SchemaFingerprint:
    return profile_table(read_file(DRIFT / v / "population.csv"))


BASE_ROWS: list[list[str | None]] = [["Rabat", "2020", "10"], ["Fes", "2021", "12"]]


# --- backlog validation test: the three required cases, by hand and through real diffs -------


def test_key_drop_is_blocking(policy: EvolutionPolicy) -> None:
    c = classify_change(ch(ChangeKind.REMOVED, "annee", type="integer", nullable=False),
                        policy=policy, primary_key=PK)  # fmt: skip
    assert c.impact is ChangeImpact.BLOCKING and c.action is EvolutionAction.BLOCK
    assert c.rule_id == "drop_key_column"

    old = fp(["region", "annee", "valeur"], BASE_ROWS)
    new = fp(["region", "valeur"], [["Rabat", "10"], ["Fes", "12"]])
    d = classify_diff(diff_schemas(old, new), policy=policy, primary_key=PK)
    assert d.overall_impact is ChangeImpact.BLOCKING and d.overall_action is EvolutionAction.BLOCK


def test_nullable_column_added_is_compatible(policy: EvolutionPolicy) -> None:
    c = classify_change(ch(ChangeKind.ADDED, "commentaire", type="string", nullable=True),
                        policy=policy, primary_key=PK)  # fmt: skip
    assert c.impact is ChangeImpact.COMPATIBLE and c.action is EvolutionAction.AUTO_APPLY

    old = fp(["region", "annee", "valeur"], BASE_ROWS)
    new = fp(["region", "annee", "valeur", "commentaire"],
             [["Rabat", "2020", "10", None], ["Fes", "2021", "12", "ok"]])  # fmt: skip
    diff = diff_schemas(old, new)
    assert [c.kind for c in diff.changes] == [ChangeKind.ADDED]
    assert diff.changes[0].detail["nullable"] is True
    d = classify_diff(diff, policy=policy, primary_key=PK)
    assert d.overall_impact is ChangeImpact.COMPATIBLE
    assert d.overall_action is EvolutionAction.AUTO_APPLY


def test_int_to_float_widening_is_compatible(policy: EvolutionPolicy) -> None:
    c = classify_change(ch(ChangeKind.TYPE_CHANGED, "valeur", **{"from": "integer", "to": "float"}),
                        policy=policy, primary_key=PK)  # fmt: skip
    assert c.impact is ChangeImpact.COMPATIBLE and c.rule_id == "type_widening"

    old = fp(["region", "annee", "valeur"], BASE_ROWS)
    new = fp(["region", "annee", "valeur"], [["Rabat", "2020", "10.5"], ["Fes", "2021", "12.25"]])
    diff = diff_schemas(old, new)
    assert [c.kind for c in diff.changes] == [ChangeKind.TYPE_CHANGED]
    d = classify_diff(diff, policy=policy, primary_key=PK)
    assert d.overall_impact is ChangeImpact.COMPATIBLE


# --- simulator v1 -> v2 -> v3 with primary key [region, annee] --------------------------------


def test_simulator_v1_to_v2_compatible(policy: EvolutionPolicy) -> None:
    d = classify_diff(diff_schemas(drift("v1"), drift("v2")), policy=policy, primary_key=PK)
    rules = {c.change.kind: c.rule_id for c in d.classified}
    assert rules == {ChangeKind.RENAMED: "rename_auto", ChangeKind.ADDED: "add_column"}
    assert d.overall_impact is ChangeImpact.COMPATIBLE
    assert d.overall_action is EvolutionAction.AUTO_APPLY


def test_simulator_v2_to_v3_review(policy: EvolutionPolicy) -> None:
    """Non-key drop + two raw format changes: REVIEW (nothing touches the key, so no BLOCK)."""
    d = classify_diff(diff_schemas(drift("v2"), drift("v3")), policy=policy, primary_key=PK)
    assert {(c.change.column, c.rule_id) for c in d.classified} == {
        ("taux_activite", "drop_column"),
        ("population_totale", "format_change"),
        ("taux_chomage", "format_change"),
    }
    assert all(c.impact is ChangeImpact.REVIEW for c in d.classified)
    assert d.overall_impact is ChangeImpact.REVIEW
    assert d.overall_action is EvolutionAction.QUEUE_REVIEW


# --- type matrix ------------------------------------------------------------------------------

TYPES = ("integer", "float", "boolean", "date", "datetime", "string", "empty")
WIDENING = {("integer", "float"), ("date", "datetime"), ("boolean", "integer")}
NARROWING = {("float", "integer"), ("datetime", "date"), ("integer", "boolean")}


def expected_shift(a: str, b: str) -> str:
    if a == "empty" or (a, b) in WIDENING:
        return "widening"
    if b == "empty" or a == "string" or (a, b) in NARROWING:
        return "narrowing"
    if b == "string":
        return "stringify"
    return "incompatible"


IMPACT_OF_SHIFT = {
    "widening": ChangeImpact.COMPATIBLE,
    "narrowing": ChangeImpact.REVIEW,
    "stringify": ChangeImpact.REVIEW,
    "incompatible": ChangeImpact.BLOCKING,
}


@pytest.mark.parametrize(("a", "b"), [(a, b) for a in TYPES for b in TYPES if a != b])
def test_type_change_matrix(policy: EvolutionPolicy, a: str, b: str) -> None:
    assert type_shift(a, b) == expected_shift(a, b)
    change = ch(ChangeKind.TYPE_CHANGED, "valeur", **{"from": a, "to": b})
    assert classify_change(change, policy=policy).impact is IMPACT_OF_SHIFT[expected_shift(a, b)]
    key_change = ch(ChangeKind.TYPE_CHANGED, "annee", **{"from": a, "to": b})
    assert classify_change(key_change, policy=policy, primary_key=PK).impact is (
        ChangeImpact.BLOCKING
    )


def test_spot_checks_of_matrix() -> None:
    assert type_shift("integer", "string") == "stringify"  # not a safe widening for calculations
    assert type_shift("string", "integer") == "narrowing"
    assert type_shift("integer", "date") == "incompatible"
    assert type_shift("integer", "decimal128") == "unknown"


def test_unknown_type_falls_back_to_default(policy: EvolutionPolicy) -> None:
    c = classify_change(ch(ChangeKind.TYPE_CHANGED, "v", **{"from": "integer", "to": "uuid"}),
                        policy=policy)  # fmt: skip
    assert c.impact is ChangeImpact.REVIEW and c.rule_id == "default"


# --- other rules ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "impact", "rule"),
    [
        (ch(ChangeKind.ADDED, "annee", type="integer", nullable=False), "blocking",
         "add_key_column"),
        (ch(ChangeKind.ADDED, "x", type="float", nullable=False), "compatible", "add_column"),
        (ch(ChangeKind.REMOVED, "x", type="float", nullable=False), "review", "drop_column"),
        (ch(ChangeKind.REMOVED, "x", type="empty", nullable=True), "compatible",
         "drop_empty_column"),
        (ch(ChangeKind.RENAMED, "pop2", "pop", decision="auto"), "compatible", "rename_auto"),
        (ch(ChangeKind.RENAMED, "pop2", "pop", decision="review"), "review", "rename_column"),
        (ch(ChangeKind.RENAMED, "an", "annee", decision="auto"), "review", "rename_key_auto"),
        (ch(ChangeKind.RENAMED, "an", "annee", decision="review"), "blocking", "rename_key_column"),
        (ch(ChangeKind.RENAMED, "annee", "an", decision="review"), "blocking", "rename_key_column"),
        (ch(ChangeKind.FORMAT_CHANGED, "region", type="string"), "review", "format_change"),
        (ch(ChangeKind.NULLABILITY_CHANGED, "x", **{"from": False, "to": True}), "compatible",
         "becomes_nullable"),
        (ch(ChangeKind.NULLABILITY_CHANGED, "annee", **{"from": False, "to": True}), "blocking",
         "key_becomes_nullable"),
        (ch(ChangeKind.NULLABILITY_CHANGED, "annee", **{"from": True, "to": False}), "compatible",
         "becomes_not_nullable"),
        (ch(ChangeKind.REORDERED, None), "compatible", "reorder"),
    ],
)  # fmt: skip
def test_rules(policy: EvolutionPolicy, change: ColumnChange, impact: str, rule: str) -> None:
    c = classify_change(change, policy=policy, primary_key=PK)
    assert (c.impact.value, c.rule_id) == (impact, rule)
    assert c.reason


def test_rename_key_through_real_diff(policy: EvolutionPolicy) -> None:
    old = fp(["region", "annee", "valeur"], BASE_ROWS)
    new = fp(["region", "annee_ref", "valeur"], BASE_ROWS)
    diff = diff_schemas(old, new)
    [ren] = [c for c in diff.changes if c.kind is ChangeKind.RENAMED]
    assert ren.old_column == "annee"
    d = classify_diff(diff, policy=policy, primary_key=PK)
    assert d.overall_impact in (ChangeImpact.REVIEW, ChangeImpact.BLOCKING)


def test_empty_primary_key_disables_key_rules(policy: EvolutionPolicy) -> None:
    c = classify_change(ch(ChangeKind.REMOVED, "annee", type="integer", nullable=False),
                        policy=policy)  # fmt: skip
    assert c.rule_id == "drop_column" and c.impact is ChangeImpact.REVIEW


def test_empty_diff_is_compatible(policy: EvolutionPolicy) -> None:
    d = classify_diff(SchemaDiff(changes=(), candidates=()), policy=policy, primary_key=PK)
    assert d.classified == ()
    assert d.overall_impact is ChangeImpact.COMPATIBLE
    assert d.overall_action is EvolutionAction.AUTO_APPLY


def test_most_severe_wins(policy: EvolutionPolicy) -> None:
    changes = (
        ch(ChangeKind.ADDED, "x", type="float", nullable=True),
        ch(ChangeKind.REMOVED, "y", type="float", nullable=False),
    )
    d = classify_diff(SchemaDiff(changes=changes, candidates=()), policy=policy, primary_key=PK)
    assert d.overall_impact is ChangeImpact.REVIEW
    blocking = (*changes, ch(ChangeKind.REMOVED, "annee", type="integer", nullable=False))
    d = classify_diff(SchemaDiff(changes=blocking, candidates=()), policy=policy, primary_key=PK)
    assert d.overall_impact is ChangeImpact.BLOCKING


def test_every_decision_is_traceable_and_deterministic(policy: EvolutionPolicy) -> None:
    rule_ids = {r.id for rules in policy.rules.values() for r in rules} | {"default"}
    for a, b in (("v1", "v2"), ("v2", "v3"), ("v1", "v3")):
        diff = diff_schemas(drift(a), drift(b))
        d1 = classify_diff(diff, policy=policy, primary_key=PK)
        assert d1 == classify_diff(diff, policy=policy, primary_key=PK)
        assert all(c.rule_id in rule_ids and c.reason for c in d1.classified)


def test_reason_holds_no_data_value(policy: EvolutionPolicy) -> None:
    diff = diff_schemas(drift("v1"), drift("v3"))
    examples = {e for c in drift("v3").columns for e in c.examples}
    for c in classify_diff(diff, policy=policy, primary_key=PK).classified:
        assert not any(e in c.reason for e in examples)


# --- H-02: rename thresholds are consumed, never re-decided -----------------------------------


def _permissive_policy(tmp_path: Path) -> EvolutionPolicy:
    text = POLICY_FILE.read_text(encoding="utf-8")
    text = text.replace(
        "    - id: rename_column\n      impact: review",
        "    - id: rename_column\n      impact: compatible",
    )
    p = tmp_path / "p.yaml"
    p.write_text(text, encoding="utf-8")
    return load_policy(p)


def test_review_rename_never_auto(tmp_path: Path) -> None:
    """Even a policy that marks every rename compatible cannot auto-apply a `review` rename."""
    permissive = _permissive_policy(tmp_path)
    c = classify_change(ch(ChangeKind.RENAMED, "b", "a", decision="review"), policy=permissive)
    assert c.impact is ChangeImpact.REVIEW and c.action is EvolutionAction.QUEUE_REVIEW
    missing = classify_change(ch(ChangeKind.RENAMED, "b", "a"), policy=permissive)
    assert missing.impact is ChangeImpact.REVIEW


# --- policy loading ---------------------------------------------------------------------------


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "policy.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_modified_policy_changes_result(tmp_path: Path) -> None:
    text = POLICY_FILE.read_text(encoding="utf-8").replace(
        "    - id: drop_column\n      impact: review",
        "    - id: drop_column\n      impact: blocking",
    )
    strict = load_policy(_write(tmp_path, text))
    c = classify_change(ch(ChangeKind.REMOVED, "x", type="float", nullable=False), policy=strict)
    assert c.impact is ChangeImpact.BLOCKING


def test_policy_must_be_total(tmp_path: Path) -> None:
    text = POLICY_FILE.read_text(encoding="utf-8")
    start = text.index("  reorder:")
    with pytest.raises(ConfigError, match=r"policy\.yaml.*reorder"):
        load_policy(_write(tmp_path, text[:start]))


def test_empty_rule_list_rejected(tmp_path: Path) -> None:
    text = POLICY_FILE.read_text(encoding="utf-8")
    start = text.index("  reorder:")
    with pytest.raises(ConfigError, match="reorder"):
        load_policy(_write(tmp_path, text[:start] + "  reorder: []\n"))


@pytest.mark.parametrize(
    ("old", "new", "field"),
    [
        ("      impact: compatible\n      reason: \"Ordre",
         "      impact: compatible\n      colour: red\n      reason: \"Ordre", "colour"),
        ("    - id: reorder\n      impact: compatible", "    - id: reorder\n      impact: maybe",
         "impact"),
        ("  rename_column:", "  rename_colum:", "rename_colum"),
        ("{column_type: empty}", "{column_type: blob}", "column_type"),
        ("{key: true, rename_decision: auto}", "{key: true, decision: auto}", "decision"),
        ("default:\n  impact: review", "default:\n  impact: compatible", "default"),
        ("{type_shift: widening}", "{nullable: true}", "nullable"),  # fact irrelevant to kind
        ("id: add_column\n", "id: drop_column\n", "drop_column"),  # duplicate rule id
    ],
)  # fmt: skip
def test_invalid_policy_rejected(tmp_path: Path, old: str, new: str, field: str) -> None:
    text = POLICY_FILE.read_text(encoding="utf-8")
    assert old in text
    with pytest.raises(ConfigError, match=rf"policy\.yaml.*{field}"):
        load_policy(_write(tmp_path, text.replace(old, new, 1)))


def test_python_tag_refused(tmp_path: Path) -> None:
    p = _write(tmp_path, "default: !!python/object/apply:os.system ['echo pwned']\n")
    with pytest.raises(ConfigError, match="cannot read YAML"):
        load_policy(p)


@pytest.mark.parametrize("text", ["", "- a\n- b\n"])
def test_non_mapping_rejected(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError, match="mapping"):
        load_policy(_write(tmp_path, text))


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="absent.yaml"):
        load_policy(tmp_path / "absent.yaml")


# --- registry interaction ---------------------------------------------------------------------


def test_with_impact_annotates_schema_changes(policy: EvolutionPolicy) -> None:
    diff = diff_schemas(drift("v2"), drift("v3"))
    rows = with_impact(classify_diff(diff, policy=policy, primary_key=PK))
    base = diff.to_schema_changes()
    assert [(r.change_type, r.column_name) for r in rows] == [
        (r.change_type, r.column_name) for r in base
    ]
    drop = next(r for r in rows if r.change_type == "drop_column")
    assert drop.detail["impact"] == "review"
    assert drop.detail["action"] == "queue_review"
    assert drop.detail["rule_id"] == "drop_column"
