"""Safe declarative type transformations (P3-05): closed-whitelist DSL, no code execution.

A rule is an ORDERED chain of steps applied to one source column to produce a typed value for a
canonical target. Rules may come from a YAML file or from an LLM proposal (ADR 0006): they are
DATA, validated before anything runs, and never interpreted as code.

Security model (the whitelist IS the defence; there is no keyword blacklist):
- Steps are a pydantic discriminated union on `op` over a CLOSED set of models (`ALLOWED_OPS`);
  `extra="forbid"`, `strict=True` (no coercion: a str-like object is never `str()`-ed, a number
  given as text is refused). An unknown op (`__import__('os')...`, `eval`, `{{7*7}}`, `${jndi:..}`)
  matches no model and is refused; every other "code-like" field is an enum (`cast.to`, units,
  `parse_date.format`, `on_missing`) or a strict pattern (`target`), so hostile text cannot fit.
- Evaluation dispatches with `match` on those model classes: no eval/exec/compile, no getattr or
  import by name, no regex supplied from outside (date formats come from a fixed list).
- Data fields (`source_col`, `map_values` keys/values, `null_if` values) are opaque strings used
  only as lookup keys or returned literally: "import"/"export" are legitimate business values.
- Bounds: steps per rule, rules per set, text length, table sizes, input value length.
- Errors never contain a row value; validation messages never echo the offending rule text.

Type flow is checked statically at validation: every chain starts as `string`; text ops need a
string, `unit_convert`/`round` need a number (so `cast` must come first). With a dictionary the
target must be a canonical field and the final type must match it (integer accepted into float).

Runtime semantics: None (null) propagates through every step; after `strip`, an empty string is
null for `cast`/`parse_date` (profiler convention). Numbers reuse the profiler parser (FR/EN,
spaces/NBSP thousands, `%`); `16,1 %` -> 16.1 (value as written), `unit_convert` percent->ratio
gives 0.161. Unit maths use Decimal for exact factors; `round` follows Python (half-even).

Tables (`transform_table`, pure): errors are per ROW (P1-06 convention). The first failing rule
sends the whole row to the dead-letter with the raw row verbatim (JSON array of the raw strings),
reason `transform_error:<source_col>:<op>` and `line_number` = 1-based row index in the RawTable
(header excluded). Invariant: rows_ok + rows_rejected == rows_read. `transform_with_deadletter`
stores rejects through `write_rejects` (idempotent) THEN applies the H-02 threshold.
"""

import json
import math
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, Self, get_args

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, model_validator

from adp.bronze.deadletter import (
    MAX_REJECT_RATE,
    TRANSFORM_ERROR,
    Reject,
    RetryPolicy,
    write_rejects,
)
from adp.bronze.store import ObjectStore
from adp.connectors.files import RawTable
from adp.errors import TransformError
from adp.logging import get_logger
from adp.mapping.dictionary import SemanticDictionary
from adp.schema.profile import BOOL_WORDS, DATE_FORMATS, is_datetime, parse_number, valid_date

log = get_logger(__name__)

MAX_STEPS = 8
MAX_RULES = 200
MAX_TEXT = 256
MAX_MAP_ENTRIES = 1000
MAX_NULL_IF = 32
MAX_VALUE_LEN = 1024

Value = str | int | float | bool | date | datetime | None
ValueType = Literal["string", "integer", "float", "boolean", "date", "datetime"]
Unit = Literal["ratio", "percent", "unit", "thousand", "million", "billion",
               "USD", "kUSD", "MUSD", "BUSD"]  # fmt: skip
DateFormat = Literal["%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y",
                     "%d.%m.%Y", "%m.%d.%Y", "%Y-%m"]  # fmt: skip

# Each unit = (dimension, value of 1 unit in the dimension base). No external rates.
_UNITS: dict[str, tuple[str, Decimal]] = {
    "ratio": ("share", Decimal(1)),
    "percent": ("share", Decimal("0.01")),
    "unit": ("count", Decimal(1)),
    "thousand": ("count", Decimal(10**3)),
    "million": ("count", Decimal(10**6)),
    "billion": ("count", Decimal(10**9)),
    "USD": ("usd", Decimal(1)),
    "kUSD": ("usd", Decimal(10**3)),
    "MUSD": ("usd", Decimal(10**6)),
    "BUSD": ("usd", Decimal(10**9)),
}
_DATE_RX: dict[str, re.Pattern[str]] = {f: rx for f, rx in DATE_FORMATS} | {
    "%Y-%m": re.compile(r"\d{4}-\d{2}")
}
_TRUE = frozenset({"true", "yes", "oui", "vrai", "1"})
_FALSE = (BOOL_WORDS | {"0"}) - _TRUE
_LOC_OK = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,40}")

_CFG = ConfigDict(extra="forbid", frozen=True, strict=True)
Text = Annotated[str, Field(max_length=MAX_TEXT)]
Target = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class Strip(BaseModel):
    """Trim whitespace (incl. NBSP, narrow and thin spaces)."""

    model_config = _CFG
    op: Literal["strip"]


class Lower(BaseModel):
    model_config = _CFG
    op: Literal["lower"]


class Upper(BaseModel):
    model_config = _CFG
    op: Literal["upper"]


class NullIf(BaseModel):
    """Sentinels -> null (compared stripped, case-insensitive)."""

    model_config = _CFG
    op: Literal["null_if"]
    values: list[Annotated[str, Field(max_length=64)]] = Field(min_length=1, max_length=MAX_NULL_IF)


class Cast(BaseModel):
    model_config = _CFG
    op: Literal["cast"]
    to: ValueType


class ParseDate(BaseModel):
    """Whitelisted date formats; ambiguity dd/mm vs mm/dd must be decided explicitly."""

    model_config = _CFG
    op: Literal["parse_date"]
    dayfirst: bool | None = None
    format: DateFormat | None = None

    @model_validator(mode="after")
    def _explicit(self) -> Self:
        if (self.dayfirst is None) == (self.format is None):
            raise ValueError("set exactly one of 'dayfirst' or 'format'")
        return self


class MapValues(BaseModel):
    """Static lookup table (exact match); `on_missing` is mandatory."""

    model_config = _CFG
    op: Literal["map_values"]
    values: dict[Text, Text | None] = Field(max_length=MAX_MAP_ENTRIES)
    on_missing: Literal["error", "keep", "null"]


class UnitConvert(BaseModel):
    model_config = _CFG
    op: Literal["unit_convert"]
    from_unit: Unit
    to_unit: Unit

    @model_validator(mode="after")
    def _same_dimension(self) -> Self:
        if self.from_unit == self.to_unit or _UNITS[self.from_unit][0] != _UNITS[self.to_unit][0]:
            raise ValueError("unsupported unit pair (allowed: ratio<->percent, "
                             "unit/thousand/million/billion, USD/kUSD/MUSD/BUSD)")  # fmt: skip
        return self


class Round(BaseModel):
    model_config = _CFG
    op: Literal["round"]
    digits: int = Field(ge=0, le=12)


_STEPS = (Strip, Lower, Upper, NullIf, Cast, ParseDate, MapValues, UnitConvert, Round)
ALLOWED_OPS: tuple[str, ...] = tuple(get_args(m.model_fields["op"].annotation)[0] for m in _STEPS)
Step = Annotated[
    Strip | Lower | Upper | NullIf | Cast | ParseDate | MapValues | UnitConvert | Round,
    Field(discriminator="op"),
]


def _flow(steps: Sequence[Step]) -> ValueType:
    """Static output type of a chain (input is raw text); ValueError on an impossible order."""
    t: ValueType = "string"
    for i, s in enumerate(steps):
        match s:
            case Strip() | Lower() | Upper() | NullIf() | MapValues() | ParseDate():
                if t != "string":
                    raise ValueError(f"step {i} ({s.op}) needs a string input, got {t}")
                t = "date" if isinstance(s, ParseDate) else "string"
            case UnitConvert() | Round():
                if t not in ("integer", "float"):
                    raise ValueError(f"step {i} ({s.op}) needs a number, got {t}; cast first")
                t = "float"
            case Cast(to=to):
                t = to
    return t


class FieldTransform(BaseModel):
    """Steps for one canonical target, independent of the source column name."""

    model_config = _CFG
    target: Target
    steps: list[Step] = Field(min_length=1, max_length=MAX_STEPS)
    _out: ValueType = PrivateAttr(default="string")

    @model_validator(mode="after")
    def _types(self) -> Self:
        self._out = _flow(self.steps)
        return self

    @property
    def output_type(self) -> ValueType:
        return self._out


class TransformRule(FieldTransform):
    """A `FieldTransform` bound to a source column (opaque name, never interpreted)."""

    source_col: str = Field(min_length=1, max_length=MAX_TEXT, pattern=r"^[^\x00-\x1f\x7f]+$")


class _RuleSet(BaseModel):
    model_config = _CFG
    rules: list[TransformRule] = Field(max_length=MAX_RULES)


class _FieldSet(BaseModel):
    model_config = _CFG
    fields: list[FieldTransform] = Field(max_length=MAX_RULES)


class StepError(TransformError):
    """A value failed one step; `op` names the step (the value is never kept)."""

    def __init__(self, op: str, message: str) -> None:
        super().__init__(f"{op}: {message}")
        self.op = op


@dataclass(frozen=True)
class TransformStats:
    rows_read: int
    rows_ok: int
    rows_rejected: int
    by_reason: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class TransformResult:
    """Typed rows (one value per target, in rule order; None = null) and dead-letter rejects."""

    columns: list[str]
    rows: list[list[Value]]
    rejects: list[Reject]
    stats: TransformStats


# --- validation ---------------------------------------------------------------------------


def _explain(exc: ValidationError, what: str) -> TransformError:
    """Readable error from loc/type only: no input, no echo of hostile keys or op names."""
    parts: list[str] = []
    for e in exc.errors(include_url=False, include_input=False, include_context=False)[:10]:
        loc = ".".join(
            str(p) if isinstance(p, int) or _LOC_OK.fullmatch(p) else "<key>" for p in e["loc"]
        )
        if e["type"] in ("union_tag_invalid", "union_tag_not_found"):
            msg = f"unknown op; allowed ops: {', '.join(ALLOWED_OPS)}"
        else:
            msg = e["msg"][:200]
        parts.append(f"{loc or '?'}: {msg}")
    return TransformError(f"invalid {what}: " + "; ".join(dict.fromkeys(parts)))


def _check(items: Sequence[FieldTransform], dictionary: SemanticDictionary | None) -> None:
    seen: set[str] = set()
    fields = {f.name: f for f in dictionary.fields} if dictionary else {}
    for i, it in enumerate(items):
        if it.target in seen:
            raise TransformError(f"rule {i}: target '{it.target}' is produced twice")
        seen.add(it.target)
        if dictionary is None:
            continue
        f = fields.get(it.target)
        if f is None:
            raise TransformError(f"rule {i}: target '{it.target}' is not a canonical field")
        out = it.output_type
        if out != f.type and not (out == "integer" and f.type == "float"):
            raise TransformError(
                f"rule {i}: steps produce {out} but '{f.name}' is {f.type} (add a cast)"
            )


def validate_rules(
    raw: object, *, dictionary: SemanticDictionary | None = None
) -> list[TransformRule]:
    """Validate rules from YAML/JSON/LLM output (a list or {'rules': [...]}) before any run."""
    if isinstance(raw, list):
        raw = {"rules": raw}
    if not isinstance(raw, dict):
        raise TransformError("invalid rules: expected a list or a mapping with 'rules'")
    try:
        rules = _RuleSet.model_validate(raw).rules
    except ValidationError as exc:
        raise _explain(exc, "rules") from exc
    _check(rules, dictionary)
    return rules


def parse_field_transforms(
    raw: object, *, dictionary: SemanticDictionary | None = None
) -> dict[str, FieldTransform]:
    """Validate {'fields': [{target, steps}, ...]} -> transforms by target."""
    if not isinstance(raw, dict):
        raise TransformError("invalid field transforms: expected a mapping with 'fields'")
    try:
        items = _FieldSet.model_validate(raw).fields
    except ValidationError as exc:
        raise _explain(exc, "field transforms") from exc
    _check(items, dictionary)
    return {f.target: f for f in items}


def load_field_transforms(
    path: Path, *, dictionary: SemanticDictionary | None = None
) -> dict[str, FieldTransform]:
    """Load a transforms YAML with `yaml.safe_load` only (python tags are refused)."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise TransformError(f"{path}: cannot read YAML ({type(exc).__name__})") from exc
    return parse_field_transforms(raw, dictionary=dictionary)


def bind_rules(
    fields: Mapping[str, FieldTransform], mapping: Mapping[str, str]
) -> list[TransformRule]:
    """Rules for an applied mapping {source_col: target}; every target needs a transform."""
    rules: list[TransformRule] = []
    for src, target in mapping.items():
        ft = fields.get(target)
        if ft is None:
            raise TransformError(f"no transform defined for target '{target[:64]}'")
        try:
            rules.append(TransformRule(source_col=src, target=target, steps=ft.steps))
        except ValidationError as exc:
            raise _explain(exc, "rule binding") from exc
    _check(rules, None)
    return rules


# --- evaluation ---------------------------------------------------------------------------


def _text(op: str, v: Value) -> str:
    if not isinstance(v, str):
        raise StepError(op, "expects a string")
    return v


def _num(op: str, v: Value) -> Decimal:
    if isinstance(v, bool) or not isinstance(v, int | float):
        raise StepError(op, "expects a number")
    return Decimal(v) if isinstance(v, int) else Decimal(repr(v))


def _finite(op: str, x: float) -> float:
    if not math.isfinite(x):
        raise StepError(op, "number out of range")
    return x


def _cast(v: Value, to: ValueType) -> Value:
    op = "cast"
    if v is None:
        return None
    if to == "string":
        if isinstance(v, str):
            return v
        return str(v).lower() if isinstance(v, bool) else (
            v.isoformat() if isinstance(v, date) else str(v)
        )  # fmt: skip
    if isinstance(v, str):
        v = v.strip()
        if not v:
            return None
    if to in ("integer", "float"):
        if isinstance(v, bool) or isinstance(v, date):
            raise StepError(op, f"cannot cast to {to}")
        if isinstance(v, str):
            parsed = parse_number(v)
            if parsed is None:
                raise StepError(op, f"not a number, cannot cast to {to}")
            if to == "integer" and "percent" in parsed[1]:
                raise StepError(op, "a percentage is not an integer")
            v = parsed[0]
        if to == "float":
            return _finite(op, float(v))
        if isinstance(v, float):
            if not v.is_integer():  # also False for inf/nan
                raise StepError(op, "not an integer")
            return int(v)
        return v
    if to == "boolean":
        if isinstance(v, bool):
            return v
        key = v.lower() if isinstance(v, str) else str(v) if isinstance(v, int) else ""
        if key in _TRUE or key in _FALSE:
            return key in _TRUE
        raise StepError(op, "not a boolean")
    if to == "date":
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, date):
            return v
        if isinstance(v, str) and valid_date(v, *DATE_FORMATS[0]):
            return date.fromisoformat(v)
        raise StepError(op, "not an ISO date (use parse_date for other formats)")
    if isinstance(v, datetime):
        return v
    if isinstance(v, str) and (is_datetime(v) or valid_date(v, *DATE_FORMATS[0])):
        return datetime.fromisoformat(v)
    raise StepError(op, "not an ISO 8601 datetime")


def _parse_date(s: str, step: ParseDate) -> date | None:
    s = s.strip()
    if not s:
        return None
    fmts: list[str]
    if step.format is not None:
        fmts = [step.format]
    else:
        first = "%d" if step.dayfirst else "%m"
        fmts = [f for f in _DATE_RX if f.startswith((first, "%Y")) and f != "%Y-%m"]
    for fmt in fmts:
        if valid_date(s, fmt, _DATE_RX[fmt]):
            return datetime.strptime(s, fmt).date()
    raise StepError("parse_date", "no whitelisted format matches")


def _apply(step: Step, v: Value) -> Value:
    match step:
        case Strip():
            return _text(step.op, v).strip()
        case Lower():
            return _text(step.op, v).lower()
        case Upper():
            return _text(step.op, v).upper()
        case NullIf(values=sentinels):
            key = _text(step.op, v).strip().casefold()
            return None if key in {x.strip().casefold() for x in sentinels} else v
        case MapValues(values=table, on_missing=missing):
            s = _text(step.op, v)
            if s in table:
                return table[s]
            if missing == "error":
                raise StepError(step.op, "value not in table")
            return s if missing == "keep" else None
        case Cast(to=to):
            return _cast(v, to)
        case ParseDate():
            return _parse_date(_text(step.op, v), step)
        case UnitConvert(from_unit=a, to_unit=b):
            x = _num(step.op, v) * _UNITS[a][1] / _UNITS[b][1]
            return _finite(step.op, float(x))
        case Round(digits=d):
            return _finite(step.op, float(round(_num(step.op, v), d)))
    raise StepError("?", "unsupported step")  # pragma: no cover - closed union


def apply_rule(rule: FieldTransform, value: Value) -> Value:
    """Apply the steps to one value (pure, deterministic); raises only TransformError."""
    if isinstance(value, str) and len(value) > MAX_VALUE_LEN:
        raise StepError("input", f"value longer than {MAX_VALUE_LEN} characters")
    for step in rule.steps:
        if value is None:
            return None
        try:
            value = _apply(step, value)
        except (ValueError, ArithmeticError) as exc:  # parsing/Decimal edge cases
            raise StepError(step.op, type(exc).__name__) from None
    return value


def transform_table(
    table: RawTable,
    rules: Sequence[TransformRule],
    *,
    source: str,
    run_id: str,
    now: Callable[[], datetime] | None = None,
) -> TransformResult:
    """Apply rules row by row; a failing row goes to the dead-letter (nothing is lost)."""
    index: dict[str, int] = {}
    for i, c in enumerate(table.columns):
        index.setdefault(c, i)
    for r in rules:
        if r.source_col not in index:
            raise TransformError(f"{source}: source column '{r.source_col[:64]}' not in table")
    ts = (now or (lambda: datetime.now(UTC)))().astimezone(UTC).isoformat()
    plan = [(r, index[r.source_col]) for r in rules]
    rows: list[list[Value]] = []
    rejects: list[Reject] = []
    for n, raw in enumerate(table.rows, start=1):
        out: list[Value] = []
        try:
            for r, i in plan:
                failing = r
                out.append(apply_rule(r, raw[i] if i < len(raw) else None))
        except StepError as exc:
            reason = f"{TRANSFORM_ERROR}:{failing.source_col}:{exc.op}"
            rejects.append(Reject(source, run_id, reason, json.dumps(raw, ensure_ascii=False),
                                  n, ts))  # fmt: skip
            continue
        rows.append(out)
    read = len(table.rows)
    if len(rows) + len(rejects) != read:  # pragma: no cover - defensive invariant
        raise TransformError(f"{source}: row accounting mismatch (read {read})")
    by_reason = dict(Counter(r.reason for r in rejects))
    log.info("transform", source=source, run_id=run_id, read=read, rejected=len(rejects),
             by_reason=by_reason)  # fmt: skip
    stats = TransformStats(read, len(rows), len(rejects), by_reason)
    return TransformResult([r.target for r in rules], rows, rejects, stats)


def transform_with_deadletter(
    table: RawTable,
    rules: Sequence[TransformRule],
    *,
    source: str,
    run_id: str,
    store: ObjectStore,
    day: date | None = None,
    now: Callable[[], datetime] | None = None,
    max_reject_rate: float = MAX_REJECT_RATE,
    retry: RetryPolicy | None = None,
) -> tuple[TransformResult, str | None]:
    """`transform_table` + idempotent reject storage, then the H-02 reject-rate threshold."""
    ts = (now or (lambda: datetime.now(UTC)))()
    res = transform_table(table, rules, source=source, run_id=run_id, now=lambda: ts)
    path = write_rejects(res.rejects, source=source, day=day or ts.date(), store=store,
                         retry=retry or RetryPolicy())  # fmt: skip
    s = res.stats
    if s.rows_read and s.rows_rejected / s.rows_read >= max_reject_rate:
        raise TransformError(
            f"{source}: {s.rows_rejected}/{s.rows_read} rows rejected by transforms "
            f"(threshold {max_reject_rate:.0%}); rejects stored at {path}"
        )
    return res, path
