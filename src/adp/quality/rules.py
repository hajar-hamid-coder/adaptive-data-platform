"""Declarative quality rules (P4-01): load/validate YAML, evaluate on typed Silver rows.

Pure and deterministic: no clock (`as_of` is injected), no I/O in `evaluate`. Results carry
counts, row INDICES and a severity, never data values (reusable by anomalies, scoring and
quarantine, P4-02..04).

Conventions:
- `None` values: `not_null` fails on them; every other rule ignores them (a missing value is a
  completeness problem, not a validity one). `unique` ignores keys containing None unless
  `ignore_null_keys: false`.
- `unique` flags the repeated occurrences, the first one is kept (quarantining both would drop a
  good row).
- A rule on an ABSENT column is `skipped` (reason in `message`); `strict=True` raises ConfigError.
- `freshness` and `sum_equals` are batch-level: one verdict (`failed_count` 1, no row indices).
- `expression` (P4-06): whitelisted predicate on declared columns (see `adp.quality.expression`).
  A NULL input makes the row "unknown": not evaluated, not failed. A row that cannot be evaluated
  (type mismatch, division by zero, bad cast) FAILS, like an incompatible type in `range`.
- `sum_equals`: sum of a numeric column vs the sum of a loaded referential field, with an absolute
  tolerance. A non-numeric non-null cell fails the batch. Details carry counts, never sums.
"""

import importlib
import math
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from adp.errors import ConfigError
from adp.mapping.dictionary import SemanticDictionary
from adp.mapping.transform import Value
from adp.quality.expression import MAX_EXPR_LEN, evaluate_predicate, parse

Severity = Literal["error", "warn"]
Status = Literal["passed", "failed", "skipped"]
RefValue = str | int
Bound = int | float | date

MAX_RULES = 200
MAX_INLINE_VALUES = 1000
MAX_REFERENCE_VALUES = 10_000
MAX_PATTERN_LEN = 200
MAX_VALUE_LEN = 512  # longer values fail the row WITHOUT running the regex
_NAME = r"^[a-z][a-z0-9_]*$"

# --- regex safety (conservative) ------------------------------------------------------------
_sre = importlib.import_module("re._parser")
_REPEATS = frozenset({"MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"})
_BANNED = frozenset({"GROUPREF", "GROUPREF_EXISTS", "ASSERT", "ASSERT_NOT", "ATOMIC_GROUP"})


def _children(name: str, av: Any) -> list[Any]:
    if name in _REPEATS:
        return [av[2]]
    if name == "SUBPATTERN":
        return [av[3]]
    if name == "BRANCH":
        return list(av[1])
    return []


def _walk(sub: Any, in_repeat: bool, unbounded: bool) -> None:
    for op, av in sub.data:
        name = str(op)
        if name in _BANNED:
            raise ValueError(f"unsupported regex construct {name} (backreference/lookaround)")
        if name == "BRANCH" and in_repeat:
            raise ValueError("alternation inside a repeat is refused (ambiguous)")
        inner_rep, inner_unb = in_repeat, unbounded
        if name in _REPEATS:
            is_unb = av[1] == _sre.MAXREPEAT
            if in_repeat and (unbounded or is_unb):
                raise ValueError("nested quantifiers are refused (catastrophic backtracking)")
            if av[1] > 1:
                inner_rep, inner_unb = True, unbounded or is_unb
        for child in _children(name, av):
            _walk(child, inner_rep, inner_unb)


def check_pattern(pattern: str) -> None:
    """Refuse long, invalid or risky patterns. CONSERVATIVE: may refuse some safe patterns.

    Refused: > 200 chars, backreferences, lookarounds, atomic groups, alternation inside a
    repeat, and nested quantifiers where either level is unbounded (`(a+)+`, `(a{1,3})*`).
    Polynomial cases like `a*a*a*` remain possible but are bounded by `MAX_VALUE_LEN`.
    """
    if len(pattern) > MAX_PATTERN_LEN:
        raise ValueError(f"pattern longer than {MAX_PATTERN_LEN} characters")
    try:
        tree = _sre.parse(pattern)
        re.compile(pattern)
    except (re.error, RecursionError, OverflowError) as exc:
        raise ValueError(f"invalid regex: {exc}") from exc
    _walk(tree, False, False)


# --- models ---------------------------------------------------------------------------------


class _Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str = Field(pattern=_NAME, max_length=64)
    severity: Severity = "error"
    description: str | None = Field(default=None, max_length=200)


class NotNull(_Rule):
    type: Literal["not_null"]
    column: str = Field(pattern=_NAME)


class Unique(_Rule):
    type: Literal["unique"]
    columns: list[str] = Field(min_length=1, max_length=10)
    ignore_null_keys: bool = True


class Range(_Rule):
    type: Literal["range"]
    column: str = Field(pattern=_NAME)
    min: Bound | None = None
    max: Bound | None = None
    inclusive: bool = True

    @model_validator(mode="after")
    def _bounds(self) -> Self:
        given = [b for b in (self.min, self.max) if b is not None]
        if not given:
            raise ValueError("range needs at least one of min/max")
        if any(isinstance(b, bool) for b in given):
            raise ValueError("bounds must be numbers or dates")
        if len({isinstance(b, date) for b in given}) > 1:
            raise ValueError("min and max must both be numbers or both dates")
        if len(given) == 2 and given[0] > given[1]:  # type: ignore[operator]
            raise ValueError("min must be <= max")
        return self


class Regex(_Rule):
    type: Literal["regex"]
    column: str = Field(pattern=_NAME)
    pattern: str

    @field_validator("pattern")
    @classmethod
    def _safe(cls, v: str) -> str:
        check_pattern(v)
        return v


class Reference(_Rule):
    """Closed referential: inline `values` OR (`reference_file`, `reference_field`)."""

    type: Literal["reference"]
    column: str = Field(pattern=_NAME)
    values: list[RefValue] | None = Field(default=None, min_length=1, max_length=MAX_INLINE_VALUES)
    reference_file: str | None = Field(default=None, pattern=_NAME)
    reference_field: str | None = Field(default=None, pattern=_NAME)

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        by_file = self.reference_file is not None or self.reference_field is not None
        if (self.values is None) == (not by_file):
            raise ValueError("give either values or reference_file+reference_field")
        if by_file and (self.reference_file is None or self.reference_field is None):
            raise ValueError("reference_file and reference_field go together")
        return self


class Freshness(_Rule):
    type: Literal["freshness"]
    column: str = Field(pattern=_NAME)
    max_age_days: int = Field(ge=0, le=36_500)


class Expression(_Rule):
    """Inter-column predicate, e.g. `date_fin >= date_debut`; checked against `columns` at load."""

    type: Literal["expression"]
    columns: list[Annotated[str, Field(pattern=_NAME)]] = Field(min_length=1, max_length=10)
    expression: str = Field(max_length=MAX_EXPR_LEN)

    @model_validator(mode="after")
    def _whitelisted(self) -> Self:
        parse(self.expression, self.columns)  # ValueError -> ValidationError -> ConfigError
        return self


class SumEquals(_Rule):
    """Batch total of `column` must equal the sum of a referential field (within `tolerance`)."""

    type: Literal["sum_equals"]
    column: str = Field(pattern=_NAME)
    reference_file: str = Field(pattern=_NAME)
    reference_field: str = Field(pattern=_NAME)
    tolerance: float = Field(default=0.0, ge=0, le=1e12)


Rule = Annotated[
    NotNull | Unique | Range | Regex | Reference | Freshness | Expression | SumEquals,
    Field(discriminator="type"),
]


class _RuleFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    rules: list[Rule] = Field(max_length=MAX_RULES)


def _columns(rule: Any) -> list[str]:
    return list(rule.columns) if isinstance(rule, Unique | Expression) else [rule.column]


def reference_key(rule: Reference | SumEquals) -> str | None:
    """Key under which `evaluate(references=...)` finds this rule's values (file rules only)."""
    if rule.reference_file is None:
        return None
    return f"{rule.reference_file}:{rule.reference_field}"


# --- loading --------------------------------------------------------------------------------


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: cannot read YAML ({type(exc).__name__})") from exc


def _fmt(exc: ValidationError) -> str:
    return "; ".join(
        f"field '{'.'.join(str(p) for p in e['loc']) or '?'}': {e['msg']}" for e in exc.errors()
    )


def load_rules(path: Path, dictionary: SemanticDictionary | None = None) -> list[Rule]:
    """Load and validate a rules file; errors name the file and field (no data value)."""
    raw = _read_yaml(path)
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a YAML mapping with a 'rules' key")
    try:
        rules = list(_RuleFile.model_validate(raw).rules)
    except ValidationError as exc:
        raise ConfigError(f"{path}: invalid quality rules: {_fmt(exc)}") from exc
    seen: set[str] = set()
    known = {f.name for f in dictionary.fields} if dictionary else None
    for r in rules:
        if r.id in seen:
            raise ConfigError(f"{path}: duplicate rule id '{r.id}'")
        seen.add(r.id)
        for col in _columns(r):
            if known is not None and col not in known:
                raise ConfigError(f"{path}: rule '{r.id}': column '{col}' not in the glossary")
    return rules


def load_reference(path: Path) -> dict[str, list[RefValue]]:
    """Load a referential file: mapping field -> non-empty bounded list of str/int."""
    raw = _read_yaml(path)
    if not isinstance(raw, dict) or not raw:
        raise ConfigError(f"{path}: expected a non-empty YAML mapping field -> list")
    out: dict[str, list[RefValue]] = {}
    for name, values in raw.items():
        ok = isinstance(values, list) and 0 < len(values) <= MAX_REFERENCE_VALUES
        if not isinstance(name, str) or not ok:
            raise ConfigError(
                f"{path}: field '{name}': expected a list of 1..{MAX_REFERENCE_VALUES}"
            )
        if any(isinstance(v, bool) or not isinstance(v, str | int) for v in values):
            raise ConfigError(f"{path}: field '{name}': values must be strings or integers")
        out[name] = list(values)
    return out


def load_references(rules: Sequence[Rule], directory: Path) -> dict[str, list[RefValue]]:
    """Resolve the file-based referentials of `rules` from `<directory>/<reference_file>.yaml`."""
    cache: dict[str, dict[str, list[RefValue]]] = {}
    out: dict[str, list[RefValue]] = {}
    for r in rules:
        if not isinstance(r, Reference | SumEquals):
            continue
        key = reference_key(r)
        if key is None:
            continue
        assert r.reference_file is not None and r.reference_field is not None  # noqa: S101
        file = cache.setdefault(
            r.reference_file, load_reference(directory / f"{r.reference_file}.yaml")
        )
        if r.reference_field not in file:
            raise ConfigError(
                f"{directory}/{r.reference_file}.yaml: rule '{r.id}': "
                f"no field '{r.reference_field}'"
            )
        out[key] = file[r.reference_field]
    return out


# --- results --------------------------------------------------------------------------------


class RuleResult(BaseModel):
    """Outcome of one rule. `failed_rows` is truncated to `max_failed_rows`; the count is exact."""

    model_config = ConfigDict(frozen=True)

    rule_id: str
    type: str
    severity: Severity
    status: Status
    level: Literal["row", "batch"]
    evaluated: int
    failed_count: int
    failed_rows: list[int]
    detail: dict[str, str | int | float | None]
    message: str


class QualityReport(BaseModel):
    """All results; `failed_row_indices` = ordered union of rows failing an `error` row rule."""

    model_config = ConfigDict(frozen=True)

    results: list[RuleResult]
    rows_total: int
    failed_row_indices: list[int]
    ok: bool


# --- evaluation -----------------------------------------------------------------------------

Cells = list[Value]
Detail = dict[str, str | int | float | None]
Outcome = tuple[int, list[int]]  # (evaluated, failed indices)


def _num_or_date(v: Value, dates: bool) -> float | date | None:
    if dates:
        if isinstance(v, datetime):
            return v.date()
        return v if isinstance(v, date) else None
    if (
        isinstance(v, bool)
        or not isinstance(v, int | float)
        or (isinstance(v, float) and math.isnan(v))
    ):
        return None
    return v


def _range(rule: Range, cells: Cells) -> Outcome:
    dates = isinstance(rule.min if rule.min is not None else rule.max, date)
    failed: list[int] = []
    n = 0
    for i, raw in enumerate(cells):
        if raw is None:
            continue
        n += 1
        v = _num_or_date(raw, dates)
        if v is None:  # incompatible type: the row fails, no exception
            failed.append(i)
            continue
        lo, hi = rule.min, rule.max
        if rule.inclusive:
            bad = (lo is not None and v < lo) or (hi is not None and v > hi)  # type: ignore[operator]
        else:
            bad = (lo is not None and v <= lo) or (hi is not None and v >= hi)  # type: ignore[operator]
        if bad:
            failed.append(i)
    return n, failed


def _matches(pattern: re.Pattern[str], value: Value) -> bool:
    text = str(value)
    if len(text) > MAX_VALUE_LEN:
        return False
    return pattern.fullmatch(text) is not None


def _unique(rule: Unique, cols: list[Cells]) -> Outcome:
    seen: set[tuple[Value, ...]] = set()
    failed: list[int] = []
    n = 0
    for i, key in enumerate(zip(*cols, strict=True)):
        if rule.ignore_null_keys and any(v is None for v in key):
            continue
        n += 1
        if key in seen:
            failed.append(i)
        seen.add(key)
    return n, failed


def _freshness(
    rule: Freshness, cells: Cells, as_of: date
) -> tuple[bool, dict[str, str | int | float | None]]:
    dated = [_num_or_date(v, True) for v in cells]
    days = [d for d in dated if isinstance(d, date)]
    detail: dict[str, str | int | float | None] = {
        "column": rule.column,
        "max_age_days": rule.max_age_days,
        "dated_rows": len(days),
        "age_days": None,
    }
    if not days:
        return False, detail
    detail["age_days"] = (as_of - max(days)).days
    return detail["age_days"] <= rule.max_age_days, detail  # type: ignore[operator]


def _expression(rule: Expression, cols: dict[str, Cells], n_rows: int) -> Outcome:
    node = parse(rule.expression, rule.columns)
    failed: list[int] = []
    evaluated = 0
    for i in range(n_rows):
        try:
            verdict = evaluate_predicate(node, {c: cols[c][i] for c in rule.columns})
        except (TypeError, ValueError, ArithmeticError):
            verdict = False  # not evaluable: the row fails (no value in any message)
        if verdict is None:
            continue
        evaluated += 1
        if not verdict:
            failed.append(i)
    return evaluated, failed


def _sum_equals(
    rule: SumEquals, cells: Cells, expected: Sequence[RefValue]
) -> tuple[bool, Detail] | None:
    """(ok, detail), or None when the referential is not numeric."""
    if any(isinstance(v, str) for v in expected):
        return None
    nums = [v for v in cells if isinstance(v, int | float) and not isinstance(v, bool)]
    present = sum(v is not None for v in cells)
    detail: Detail = {"column": rule.column, "tolerance": rule.tolerance, "rows_summed": len(nums)}
    total, target = math.fsum(nums), math.fsum(v for v in expected if isinstance(v, int))
    ok = len(nums) == present and abs(total - target) <= rule.tolerance
    return ok, detail


def _skipped(
    base: dict[str, Any], level: Literal["row", "batch"], detail: Detail, why: str
) -> RuleResult:
    return RuleResult(
        **base,
        status="skipped",
        level=level,
        evaluated=0,
        failed_count=0,
        failed_rows=[],
        detail=detail,
        message=f"Règle ignorée : {why}.",
    )


def evaluate(
    rules: Sequence[Rule],
    columns: Sequence[str],
    rows: Sequence[Sequence[Value]],
    *,
    as_of: date | datetime | None = None,
    references: Mapping[str, Sequence[RefValue]] | None = None,
    strict: bool = False,
    max_failed_rows: int = 1000,
) -> QualityReport:
    """Evaluate `rules` on typed rows. Never raises on data; `strict` raises on absent columns."""
    index = {c: i for i, c in enumerate(columns)}
    width = len(columns)
    n_rows = len(rows)
    refs = references or {}
    day = as_of.date() if isinstance(as_of, datetime) else as_of
    cache: dict[str, Cells] = {}

    def col(name: str) -> Cells:
        if name not in cache:
            j = index[name]
            cache[name] = [r[j] if len(r) == width else None for r in rows]
        return cache[name]

    results: list[RuleResult] = []
    union: set[int] = set()
    for rule in rules:
        base: dict[str, Any] = {"rule_id": rule.id, "type": rule.type, "severity": rule.severity}
        level: Literal["row", "batch"] = (
            "batch" if isinstance(rule, Freshness | SumEquals) else "row"
        )
        detail: dict[str, str | int | float | None] = {"column": ", ".join(_columns(rule))}

        missing = [c for c in _columns(rule) if c not in index]
        if missing:
            if strict:
                raise ConfigError(f"rule '{rule.id}': absent column '{missing[0]}'")
            results.append(_skipped(base, level, detail, f"colonne absente ({missing[0]})"))
            continue

        failed: list[int] = []
        evaluated = 0
        ok = True
        match rule:
            case NotNull():
                cells = col(rule.column)
                evaluated, failed = n_rows, [i for i, v in enumerate(cells) if v is None]
            case Unique():
                evaluated, failed = _unique(rule, [col(c) for c in rule.columns])
            case Range():
                evaluated, failed = _range(rule, col(rule.column))
            case Regex():
                pat = re.compile(rule.pattern)
                cells = col(rule.column)
                evaluated = sum(v is not None for v in cells)
                failed = [i for i, v in enumerate(cells) if v is not None and not _matches(pat, v)]
            case Expression():
                evaluated, failed = _expression(rule, {c: col(c) for c in rule.columns}, n_rows)
            case SumEquals():
                allowed = refs.get(str(reference_key(rule)))
                if allowed is None:
                    results.append(_skipped(base, level, detail, "référentiel non fourni"))
                    continue
                checked = _sum_equals(rule, col(rule.column), allowed)
                if checked is None:
                    results.append(_skipped(base, level, detail, "référentiel non numérique"))
                    continue
                ok, detail = checked
                evaluated = 1
            case Reference():
                key = reference_key(rule)
                allowed = rule.values if key is None else refs.get(key)
                if allowed is None:
                    results.append(_skipped(base, level, detail, "référentiel non fourni"))
                    continue
                allowed_set = set(allowed)
                cells = col(rule.column)
                evaluated = sum(v is not None for v in cells)
                failed = [i for i, v in enumerate(cells) if v is not None and v not in allowed_set]
            case Freshness():
                if day is None:
                    results.append(_skipped(base, level, detail, "as_of non fourni"))
                    continue
                ok, detail = _freshness(rule, col(rule.column), day)
                evaluated, failed = 1, []
        failed_count = len(failed) if level == "row" else (0 if ok else 1)
        if level == "row" and rule.severity == "error":
            union.update(failed)
        status: Status = "passed" if failed_count == 0 else "failed"
        if status == "passed":
            msg = "Règle respectée."
        elif isinstance(rule, SumEquals):
            msg = f"Total de la colonne {rule.column} différent du référentiel (hors tolérance)."
        elif level == "batch":
            msg = f"Lot trop ancien ou sans date exploitable (colonne {detail['column']})."
        else:
            msg = f"{failed_count} ligne(s) sur {evaluated} en échec (colonne {detail['column']})."
        results.append(RuleResult(**base, status=status, level=level, evaluated=evaluated,
                                  failed_count=failed_count, failed_rows=failed[:max_failed_rows],
                                  detail=detail, message=msg))  # fmt: skip
    blocking = any(r.status == "failed" and r.severity == "error" for r in results)
    return QualityReport(results=results, rows_total=n_rows, failed_row_indices=sorted(union),
                         ok=not blocking)  # fmt: skip
