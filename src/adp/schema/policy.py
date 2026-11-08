"""Impact classification of schema changes and evolution policy (P2-04).

Each `ColumnChange` of a `SchemaDiff` gets an impact (`ChangeImpact`: compatible / à revoir /
bloquant) and the automatic action derived from it (`EvolutionAction`, fixed mapping):
COMPATIBLE -> AUTO_APPLY, REVIEW -> QUEUE_REVIEW, BLOCKING -> BLOCK (batch stopped or
quarantined by the caller, never a silent change). The overall decision of a diff is the most
severe one (BLOCKING > REVIEW > COMPATIBLE); an empty diff is COMPATIBLE / AUTO_APPLY.
This module only decides; wiring to Dagster or quarantine is done elsewhere.

Policy (`config/evolution_policy.yaml`) = configuration, never code: loaded with
`yaml.safe_load` only, validated by pydantic (extra=forbid). Per ChangeKind, an ordered list of
rules; the first rule whose `when` facts all match wins. Facts are computed here from the change
and the source context (no DSL, no eval). Load-time guarantees: every ChangeKind has at least
one rule (the policy is total), rule ids are unique (audit trail), a condition must be
meaningful for its kind (a fact that can never be set is a config bug), the fallback `default`
(used when no rule matches, e.g. an unknown type) is REVIEW or BLOCKING, never COMPATIBLE.
`reason` is static text from the policy: no data value is ever interpolated.

Primary key: a PARAMETER (`SourceSpec.primary_key`). A change touches the key when its new or
old column name is in it (a rename of `annee` is a key change). Empty key = key rules inactive.

Rename thresholds (H-02): owned by `DiffConfig` only. The policy consumes the decision the diff
already computed (`detail["decision"]`: auto / review) and never re-scores, so the thresholds
live in one place. Guardrail in code, whatever the YAML says: a rename whose decision is not
`auto` is at least REVIEW (a `review` rename can never be auto-applied).

Type matrix (`type_shift(from, to)`), from the downstream (Silver) point of view:
- widening: the new type holds every old value with the same meaning -> integer->float,
  date->datetime, boolean->integer (true/false -> 1/0), empty->anything (no old value).
- narrowing: old values may not fit -> float->integer, datetime->date, integer->boolean,
  string->anything, anything->empty (the column lost all its values).
- stringify: anything->string: lossless as text but breaks numeric/temporal calculations,
  so NOT a safe widening.
- incompatible: everything else (integer<->date, boolean<->float...): no deterministic cast.
- unknown: a type outside the profiler vocabulary -> no rule matches -> `default`.
"""

from collections.abc import Collection
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self, get_args

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from adp.errors import ConfigError
from adp.schema.diff import ChangeKind, ColumnChange, SchemaDiff
from adp.schema.profile import InferredType
from adp.schema.registry import SchemaChange

TypeShift = Literal["widening", "narrowing", "stringify", "incompatible", "unknown"]

_KNOWN_TYPES = frozenset(get_args(InferredType))
_WIDENING = frozenset({("integer", "float"), ("date", "datetime"), ("boolean", "integer")})
_NARROWING = frozenset({("float", "integer"), ("datetime", "date"), ("integer", "boolean")})


class ChangeImpact(StrEnum):
    """Impact of a change: compatible / à revoir / bloquant."""

    COMPATIBLE = "compatible"
    REVIEW = "review"
    BLOCKING = "blocking"


class EvolutionAction(StrEnum):
    """Automatic action derived from the impact."""

    AUTO_APPLY = "auto_apply"
    QUEUE_REVIEW = "queue_review"
    BLOCK = "block"


_SEVERITY = {ChangeImpact.COMPATIBLE: 0, ChangeImpact.REVIEW: 1, ChangeImpact.BLOCKING: 2}
_ACTION = {
    ChangeImpact.COMPATIBLE: EvolutionAction.AUTO_APPLY,
    ChangeImpact.REVIEW: EvolutionAction.QUEUE_REVIEW,
    ChangeImpact.BLOCKING: EvolutionAction.BLOCK,
}

# facts that can be set for each kind (`key` is meaningful for every kind but reorder)
_FACTS: dict[ChangeKind, frozenset[str]] = {
    ChangeKind.ADDED: frozenset({"key", "nullable", "column_type"}),
    ChangeKind.REMOVED: frozenset({"key", "nullable", "column_type"}),
    ChangeKind.RENAMED: frozenset({"key", "rename_decision"}),
    ChangeKind.TYPE_CHANGED: frozenset({"key", "type_shift"}),
    ChangeKind.FORMAT_CHANGED: frozenset({"key", "column_type"}),
    ChangeKind.NULLABILITY_CHANGED: frozenset({"key", "nullable"}),
    ChangeKind.REORDERED: frozenset(),
}


def type_shift(old: str, new: str) -> TypeShift:
    """Category of a type change (matrix in the module doc)."""
    if old not in _KNOWN_TYPES or new not in _KNOWN_TYPES:
        return "unknown"
    if old == "empty" or (old, new) in _WIDENING:
        return "widening"
    if new == "empty" or old == "string" or (old, new) in _NARROWING:
        return "narrowing"
    if new == "string":
        return "stringify"
    return "incompatible"


class Conditions(BaseModel):
    """Facts a change must match (AND); an unset field is not checked."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: bool | None = None
    nullable: bool | None = None
    column_type: InferredType | None = None
    rename_decision: Literal["auto", "review"] | None = None
    type_shift: Literal["widening", "narrowing", "stringify", "incompatible"] | None = None

    def matches(self, facts: dict[str, Any]) -> bool:
        return all(facts.get(k) == v for k, v in self.model_dump(exclude_none=True).items())


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    when: Conditions = Conditions()
    impact: ChangeImpact
    reason: str = Field(min_length=1, max_length=300)


class DefaultRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    impact: Literal[ChangeImpact.REVIEW, ChangeImpact.BLOCKING] = ChangeImpact.REVIEW
    reason: str = Field(default="Not covered by the policy: human review.", min_length=1)


class EvolutionPolicy(BaseModel):
    """Ordered rules per ChangeKind plus a conservative fallback."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    default: DefaultRule = DefaultRule()
    rules: dict[ChangeKind, list[Rule]]

    @model_validator(mode="after")
    def _check(self) -> Self:
        missing = [k.value for k in ChangeKind if not self.rules.get(k)]
        if missing:
            raise ValueError(f"missing rules for change kinds: {', '.join(missing)}")
        seen: set[str] = {"default"}
        for kind, rules in self.rules.items():
            for r in rules:
                if r.id in seen:
                    raise ValueError(f"duplicate rule id: {r.id}")
                seen.add(r.id)
                extra = set(r.when.model_dump(exclude_none=True)) - _FACTS[kind]
                if extra:
                    raise ValueError(
                        f"rule {r.id}: condition(s) {', '.join(sorted(extra))} "
                        f"never set for {kind.value}"
                    )
        return self


class ClassifiedChange(BaseModel):
    """A change with its impact, action and the rule that decided it."""

    model_config = ConfigDict(frozen=True)

    change: ColumnChange
    impact: ChangeImpact
    action: EvolutionAction
    rule_id: str
    reason: str


class DiffImpact(BaseModel):
    """Classified changes of a diff and the overall (most severe) decision."""

    model_config = ConfigDict(frozen=True)

    classified: tuple[ClassifiedChange, ...]
    overall_impact: ChangeImpact
    overall_action: EvolutionAction


def _facts(change: ColumnChange, primary_key: Collection[str]) -> dict[str, Any]:
    d = change.detail
    names = {n for n in (change.column, change.old_column) if n is not None}
    facts: dict[str, Any] = {"key": bool(names & set(primary_key))}
    match change.kind:
        case ChangeKind.ADDED | ChangeKind.REMOVED:
            facts |= {"nullable": d.get("nullable"), "column_type": d.get("type")}
        case ChangeKind.RENAMED:
            facts["rename_decision"] = d.get("decision")
        case ChangeKind.TYPE_CHANGED:
            facts["type_shift"] = type_shift(str(d.get("from")), str(d.get("to")))
        case ChangeKind.FORMAT_CHANGED:
            facts["column_type"] = d.get("type")
        case ChangeKind.NULLABILITY_CHANGED:
            facts["nullable"] = d.get("to")
    return facts


def classify_change(
    change: ColumnChange, *, policy: EvolutionPolicy, primary_key: Collection[str] = ()
) -> ClassifiedChange:
    """Impact of one change: first matching rule of its kind, else `policy.default`."""
    facts = _facts(change, primary_key)
    rule = next((r for r in policy.rules[change.kind] if r.when.matches(facts)), None)
    impact, rule_id, reason = (
        (rule.impact, rule.id, rule.reason)
        if rule
        else (policy.default.impact, "default", policy.default.reason)
    )
    if (
        change.kind is ChangeKind.RENAMED
        and facts["rename_decision"] != "auto"
        and impact is ChangeImpact.COMPATIBLE
    ):
        impact = ChangeImpact.REVIEW  # H-02 guardrail: only the diff can make a rename auto
    return ClassifiedChange(
        change=change, impact=impact, action=_ACTION[impact], rule_id=rule_id, reason=reason
    )


def classify_diff(
    diff: SchemaDiff, *, policy: EvolutionPolicy, primary_key: Collection[str] = ()
) -> DiffImpact:
    """Classify every change; the overall decision is the most severe impact."""
    classified = tuple(
        classify_change(c, policy=policy, primary_key=primary_key) for c in diff.changes
    )
    overall = max((c.impact for c in classified), key=_SEVERITY.__getitem__,
                  default=ChangeImpact.COMPATIBLE)  # fmt: skip
    return DiffImpact(
        classified=classified, overall_impact=overall, overall_action=_ACTION[overall]
    )


def with_impact(result: DiffImpact) -> list[SchemaChange]:
    """`SchemaRegistry.record_changes` rows with impact, action and rule_id in `detail`."""
    diff = SchemaDiff(changes=tuple(c.change for c in result.classified), candidates=())
    rows = diff.to_schema_changes()
    return [
        row.model_copy(
            update={
                "detail": row.detail
                | {"impact": c.impact.value, "action": c.action.value, "rule_id": c.rule_id}
            }  # fmt: skip
        )
        for row, c in zip(rows, result.classified, strict=True)
    ]


def load_policy(path: Path) -> EvolutionPolicy:
    """Load and validate the evolution policy YAML; errors name the file and field."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: cannot read YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a YAML mapping at top level")
    try:
        return EvolutionPolicy.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(
            f"field '{'.'.join(str(p) for p in e['loc']) or '?'}': {e['msg']}" for e in exc.errors()
        )
        raise ConfigError(f"{path}: invalid evolution policy: {details}") from exc
