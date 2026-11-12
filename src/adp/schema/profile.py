"""Column profiling and structural schema fingerprint over a `RawTable`.

Privacy (H-09): `ColumnProfile.examples` may contain real data. They must never be sent
to an LLM without prior anonymisation (out of scope here); use `max_examples=0` to omit them.

Inference rules (all documented decisions):
- Null = None, empty or whitespace-only. Values are stripped before typing.
- Type = first matching of boolean, number (integer/float), datetime, date whose share of
  non-null values is >= `min_type_confidence`; else string. `type_confidence` is that share
  (for a string column: best non-string share if any matched, else 1.0).
- Numbers: FR/EN decimals (`12,5`, `12.5`), thousands separators (space, NBSP, narrow NBSP,
  `1,234.56`, `1.234,56`), sign, simple scientific notation, trailing `%` (value kept as
  written, 16,1 % -> 16.1, always float). Ambiguity: `1,234` is read as EN thousands
  (integer 1234) while `1.234` is a decimal (float 1.234); `1.234.567` is FR thousands.
  Integer-looking values with a leading zero (`007`, phone numbers) are strings.
  The column is integer only when every numeric value is an integer, else float.
- Dates: ISO `YYYY-MM-DD`, `YYYY/MM/DD`, `dd/mm/yyyy` (also `-` and `.`). Day-first by default;
  month-first only if strictly more values are valid as mm/dd than as dd/mm (e.g. `12/25/2024`).
  The chosen strptime format is exposed in `format_hint`. Datetimes are ISO 8601 (with or
  without time zone; date-only ISO values are tolerated in a datetime column). A bare year
  (`2019`) is an integer, never a date.
- Boolean: true/false, yes/no, oui/non, vrai/faux (any case); `0`/`1` only when the column
  holds exactly those two values (a column like 0,1,2 or all 1 stays integer).
- Fingerprint: sha256 over (name, inferred type, nullable) per column in order; independent
  of values, examples, counts, row order. `fingerprint_unordered` sorts columns first.
"""

import hashlib
import json
import re
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from adp.connectors.files import RawTable
from adp.errors import SchemaError

InferredType = Literal["integer", "float", "boolean", "date", "datetime", "string", "empty"]

_SP = "    "
_NUM_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\d+(?:[.,]\d+)?[eE][+-]?\d+"), ""),
    (re.compile(rf"[1-9]\d{{0,2}}(?:[{_SP}]\d{{3}})+(?:[.,]\d+)?"), "thousands_space"),
    (re.compile(r"[1-9]\d{0,2}(?:,\d{3})+(?:\.\d+)?"), "thousands_comma"),
    (
        re.compile(r"[1-9]\d{0,2}(?:\.\d{3}){2,}(?:,\d+)?|[1-9]\d{0,2}(?:\.\d{3})+,\d+"),
        "thousands_dot",
    ),
    (re.compile(r"\d+(?:[.,]\d+)?"), ""),
]
BOOL_WORDS = {"true", "false", "yes", "no", "oui", "non", "vrai", "faux"}
DATE_FORMATS = [
    ("%Y-%m-%d", re.compile(r"\d{4}-\d{2}-\d{2}")),
    ("%Y/%m/%d", re.compile(r"\d{4}/\d{2}/\d{2}")),
    ("%d/%m/%Y", re.compile(r"\d{2}/\d{2}/\d{4}")),
    ("%m/%d/%Y", re.compile(r"\d{2}/\d{2}/\d{4}")),
    ("%d-%m-%Y", re.compile(r"\d{2}-\d{2}-\d{4}")),
    ("%m-%d-%Y", re.compile(r"\d{2}-\d{2}-\d{4}")),
    ("%d.%m.%Y", re.compile(r"\d{2}\.\d{2}\.\d{4}")),
    ("%m.%d.%Y", re.compile(r"\d{2}\.\d{2}\.\d{4}")),
]
_DATETIME_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"
)


class ColumnProfile(BaseModel):
    """Profile of one column. `examples` are real values: never send them to an LLM as is."""

    model_config = ConfigDict(frozen=True)

    name: str
    inferred_type: InferredType
    type_confidence: float
    nullable: bool
    null_count: int
    row_count: int
    distinct_count: int
    min: float | None = None
    max: float | None = None
    mean: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    examples: tuple[str, ...] = ()
    format_hint: str | None = None


class SchemaFingerprint(BaseModel):
    """Column profiles plus structure-only digests (see module docstring)."""

    model_config = ConfigDict(frozen=True)

    columns: list[ColumnProfile]
    fingerprint: str
    fingerprint_unordered: str


def parse_number(s: str) -> tuple[int | float, frozenset[str], bool] | None:
    """Parse a FR/EN number -> (value, hint tokens, is_integer), or None."""
    tokens: set[str] = set()
    if s.endswith("%"):
        s = s[:-1].rstrip(_SP)
        tokens.add("percent")
    neg = False
    if s[:1] in "+-−":
        neg = s[0] in "-−"
        s = s[1:]
    if not s or s[0] in _SP:
        return None
    if re.fullmatch(r"0\d+", s):
        return None
    match = [tok for pat, tok in _NUM_PATTERNS if pat.fullmatch(s)]
    if not match:
        return None
    tok = match[0]
    if tok:
        tokens.add(tok)
    if tok == "thousands_comma":
        body = s.replace(",", "")
    elif tok == "thousands_dot":
        body = s.replace(".", "").replace(",", ".")
        if "," in s:
            tokens.add("decimal_comma")
    else:
        body = re.sub(f"[{_SP}]", "", s)
        if "," in body:
            tokens.add("decimal_comma")
            body = body.replace(",", ".")
    is_int = re.fullmatch(r"\d+", body) is not None and "percent" not in tokens
    val: int | float = int(body) if is_int else float(body)
    return (-val if neg else val), frozenset(tokens), is_int


def valid_date(v: str, fmt: str, rx: re.Pattern[str]) -> bool:
    if not rx.fullmatch(v):
        return False
    try:
        datetime.strptime(v, fmt)
    except ValueError:
        return False
    return True


def is_datetime(v: str) -> bool:
    if not _DATETIME_RE.fullmatch(v):
        return False
    try:
        datetime.fromisoformat(v)
    except ValueError:
        return False
    return True


def _date_hint_from(counter: Counter[str]) -> tuple[str, int]:
    """Best date format (ties -> day-first) and number of values valid under it."""
    best_fmt, best = DATE_FORMATS[0][0], 0
    for fmt, rx in DATE_FORMATS:
        n = sum(c for v, c in counter.items() if valid_date(v, fmt, rx))
        if n > best:
            best_fmt, best = fmt, n
    return best_fmt, best


def profile_column(
    name: str,
    values: Sequence[str | None],
    *,
    max_examples: int = 5,
    min_type_confidence: float = 0.95,
) -> ColumnProfile:
    """Profile one column of raw strings (pure, deterministic)."""
    if max_examples < 0:
        raise SchemaError("max_examples must be >= 0")
    if not 0 < min_type_confidence <= 1:
        raise SchemaError("min_type_confidence must be in (0, 1]")
    vals = [v.strip() for v in values if v is not None and v.strip()]
    counter = Counter(vals)
    total = len(vals)
    base: dict[str, object] = {
        "name": name,
        "nullable": len(values) > total,
        "null_count": len(values) - total,
        "row_count": len(values),
        "distinct_count": len(counter),
    }
    if total == 0:
        return ColumnProfile.model_validate(
            {**base, "inferred_type": "empty", "type_confidence": 1.0}
        )

    examples = tuple(
        v for v, _ in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:max_examples]
    )
    thr = min_type_confidence

    def make(
        typ: InferredType, conf: float, hint: str | None = None, **extra: float | int | None
    ) -> ColumnProfile:
        return ColumnProfile.model_validate(
            {
                **base,
                "inferred_type": typ,
                "type_confidence": conf,
                "examples": examples,
                "format_hint": hint,
                **extra,
            }
        )

    # boolean
    words = sum(c for v, c in counter.items() if v.lower() in BOOL_WORDS)
    if set(counter) == {"0", "1"}:
        return make("boolean", 1.0, "0/1")
    if words / total >= thr:
        return make("boolean", words / total)

    # numbers
    parsed = {v: parse_number(v) for v in counter}
    nums = {v: p for v, p in parsed.items() if p is not None}
    num_share = sum(counter[v] for v in nums) / total
    if num_share >= thr:
        all_int = all(p[2] for p in nums.values())
        n = sum(counter[v] for v in nums)
        mean = sum(p[0] * counter[v] for v, p in nums.items()) / n
        tokens = sorted({t for p in nums.values() for t in p[1]})
        return make(
            "integer" if all_int else "float",
            num_share,
            ",".join(tokens) or None,
            min=float(min(p[0] for p in nums.values())),
            max=float(max(p[0] for p in nums.values())),
            mean=mean,
        )

    # datetime / date
    dt = sum(c for v, c in counter.items() if is_datetime(v))
    iso_date = sum(c for v, c in counter.items() if valid_date(v, *DATE_FORMATS[0]))
    dt_share = (dt + iso_date) / total if dt else 0.0
    fmt, n_date = _date_hint_from(counter)
    date_share = n_date / total
    if dt_share >= thr:
        return make("datetime", dt_share, "iso8601")
    if date_share >= thr:
        return make("date", date_share, fmt)

    best = max(words / total, num_share, dt_share, date_share)
    lengths = [len(v) for v in vals]
    return make(
        "string", best if best > 0 else 1.0, None, min_length=min(lengths), max_length=max(lengths)
    )


def _digest(items: list[list[object]]) -> str:
    raw = json.dumps(items, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def profile_table(
    table: RawTable, *, max_examples: int = 5, min_type_confidence: float = 0.95
) -> SchemaFingerprint:
    """Profile every column of `table` and compute the structural fingerprints."""
    cols = [
        profile_column(
            name,
            [r[i] if i < len(r) else None for r in table.rows],
            max_examples=max_examples,
            min_type_confidence=min_type_confidence,
        )
        for i, name in enumerate(table.columns)
    ]
    items: list[list[object]] = [[c.name, c.inferred_type, c.nullable] for c in cols]
    return SchemaFingerprint(
        columns=cols,
        fingerprint=_digest(items),
        fingerprint_unordered=_digest(sorted(items, key=lambda i: json.dumps(i))),
    )
