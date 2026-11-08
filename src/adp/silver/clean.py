"""Silver cleaning (P3-06): text normalisation per canonical field, then P3-05 typing.

Pipeline per cell: (1) text cleaning here: invisible characters removed, any whitespace (NBSP,
U+202F, tabs) collapsed and trimmed, Unicode NFC, missing-value tokens -> null, per-field case
(`config/semantic/cleaning.yaml`); (2) typing/units/dates through the P3-05 DSL
(`transform_table`, nothing re-implemented), per-row dead-letter and H-02 reject threshold.

Accents are KEPT in displayed values; an accent-free join key `<field>_key` is produced only when
`CleaningPolicy(join_keys=True)` and the field asks for it. Unmapped source columns are ignored
and listed in `CleanResult.unmapped`.

The correction log holds counters by (field, kind) and a bounded sample of ROW NUMBERS: never a
value, before or after, so it can be persisted and logged.
"""

import json
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from adp.bronze.deadletter import MAX_REJECT_RATE, Reject, RetryPolicy, write_rejects
from adp.bronze.store import ObjectStore
from adp.connectors.files import RawTable
from adp.errors import ConfigError, TransformError
from adp.logging import get_logger
from adp.mapping.dictionary import SemanticDictionary, normalize
from adp.mapping.transform import (
    FieldTransform,
    TransformStats,
    UnitConvert,
    Value,
    bind_rules,
    transform_table,
)

log = get_logger(__name__)

SAMPLE_ROWS = 5
NULL_TOKENS = ("", "-", "N/A", "null", "..")
_PLAIN_NUMBER = re.compile(r"-?\d+(\.\d+)?")
_PARTICLES = frozenset({"de", "du", "des", "la", "le", "les", "et", "sur", "sous", "en", "d", "l"})
_WORD_SEP = re.compile(r"([\s\-'’]+)")

KINDS = {
    "control_chars_removed": "caractères invisibles ou de contrôle supprimés",
    "whitespace_collapsed": "espaces normalisés",
    "unicode_normalized": "Unicode normalisé (NFC)",
    "case_normalized": "casse normalisée",
    "sentinel_to_null": "valeur manquante convertie en null",
    "date_to_iso": "date convertie en ISO 8601",
    "number_locale_parsed": "nombre FR/EN converti",
    "unit_converted": "unité convertie vers l'unité canonique",
}


class TextRule(BaseModel):
    """Text cleaning rule of one canonical field."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    target: str
    case: Literal["keep", "upper", "lower", "title"] = "keep"
    accent_key: bool = False


class _CleaningFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: list[TextRule]


@dataclass(frozen=True)
class CleaningPolicy:
    null_tokens: tuple[str, ...] = NULL_TOKENS  # compared case-insensitively, after trimming
    join_keys: bool = False
    max_reject_rate: float = MAX_REJECT_RATE


def parse_cleaning(raw: object, dictionary: SemanticDictionary) -> dict[str, TextRule]:
    """Validate rules; the policy must be total on string fields and name known fields only."""
    try:
        rules = _CleaningFile.model_validate(raw).fields
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise ConfigError(f"invalid cleaning rules: {details}") from exc
    types = {f.name: f.type for f in dictionary.fields}
    out: dict[str, TextRule] = {}
    for r in rules:
        if r.target not in types:
            raise ConfigError(f"cleaning rule for unknown field '{r.target[:64]}'")
        if types[r.target] != "string":
            raise ConfigError(f"cleaning rule on non-string field '{r.target}'")
        if r.target in out:
            raise ConfigError(f"duplicate cleaning rule for '{r.target}'")
        out[r.target] = r
    missing = sorted(n for n, t in types.items() if t == "string" and n not in out)
    if missing:
        raise ConfigError(f"no cleaning rule for string fields: {', '.join(missing)}")
    return out


def load_cleaning(path: Path, dictionary: SemanticDictionary) -> dict[str, TextRule]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: cannot read YAML ({type(exc).__name__})") from exc
    return parse_cleaning(raw, dictionary)


# --- correction log -------------------------------------------------------------------------


@dataclass
class CorrectionLog:
    """Counters by (field, kind) and bounded sample of row numbers; never any value."""

    counts: Counter[tuple[str, str]] = field(default_factory=Counter)
    rows: dict[tuple[str, str], list[int]] = field(default_factory=dict)

    def add(self, target: str, kind: str, row: int) -> None:
        key = (target, kind)
        self.counts[key] += 1
        sample = self.rows.setdefault(key, [])
        if len(sample) < SAMPLE_ROWS:
            sample.append(row)

    def to_json(self) -> str:
        items = [
            {"field": t, "kind": k, "count": n, "sample_rows": self.rows[(t, k)]}
            for (t, k), n in sorted(self.counts.items())
        ]
        return json.dumps(items, ensure_ascii=False, sort_keys=True)


def render_log(log_: CorrectionLog) -> str:
    """Human-readable French summary, counts only."""
    if not log_.counts:
        return "Aucune correction."
    lines = []
    for (t, k), n in sorted(log_.counts.items()):
        rows = ", ".join(map(str, log_.rows[(t, k)]))
        lines.append(f"- {t} : {KINDS.get(k, k)} : {n} valeur(s) (lignes ex. : {rows})")
    return "\n".join(lines)


# --- text cleaning ---------------------------------------------------------------------------


def _invisible(c: str) -> bool:
    cat = unicodedata.category(c)
    return cat == "Cf" or (cat == "Cc" and not c.isspace())


def _title(s: str) -> str:
    parts = _WORD_SEP.split(s)
    for i in range(0, len(parts), 2):
        w = parts[i].lower()
        parts[i] = w if i > 0 and w in _PARTICLES else w[:1].upper() + w[1:]
    return "".join(parts)


def _case(s: str, mode: str) -> str:
    match mode:
        case "upper":
            return s.upper()
        case "lower":
            return s.lower()
        case "title":
            return _title(s)
    return s


def clean_text(
    s: str, rule: TextRule | None, null_tokens: frozenset[str]
) -> tuple[str | None, list[str]]:
    """Clean one string (pure); returns (value or None, kinds of corrections really applied)."""
    kinds: list[str] = []
    t = "".join(c for c in s if not _invisible(c))
    if t != s:
        kinds.append("control_chars_removed")
    u = " ".join(t.split())
    if u != t:
        kinds.append("whitespace_collapsed")
    n = unicodedata.normalize("NFC", u)
    if n != u:
        kinds.append("unicode_normalized")
    if n.casefold() in null_tokens:
        return None, ["sentinel_to_null"]
    c = _case(n, rule.case if rule else "keep")
    if c != n:
        kinds.append("case_normalized")
    return unicodedata.normalize("NFC", c), kinds


# --- table cleaning --------------------------------------------------------------------------


@dataclass(frozen=True)
class CleanResult:
    columns: list[str]  # canonical names (+ `<field>_key` columns when requested)
    rows: list[list[Value]]
    corrections: CorrectionLog
    rejects: list[Reject]
    stats: TransformStats
    unmapped: list[str] = field(default_factory=list)
    rejects_path: str | None = None


def _converted(target: str, ft: FieldTransform, s: str | None, v: Value,
               ) -> str | None:  # fmt: skip
    """Kind of typing correction really applied (None when the text was already canonical)."""
    if s is None or v is None:
        return None
    if any(isinstance(st, UnitConvert) for st in ft.steps):
        return "unit_converted"
    if isinstance(v, date):  # datetime is a date
        return None if s == v.isoformat() else "date_to_iso"
    if isinstance(v, int | float) and not isinstance(v, bool):
        return None if _PLAIN_NUMBER.fullmatch(s) else "number_locale_parsed"
    return None


def clean_table(
    table: RawTable,
    mapping: Mapping[str, str],
    *,
    dictionary: SemanticDictionary,
    transforms: Mapping[str, FieldTransform],
    text_rules: Mapping[str, TextRule],
    policy: CleaningPolicy = CleaningPolicy(),  # noqa: B008
    source: str,
    run_id: str,
    store: ObjectStore | None = None,
    day: date | None = None,
    now: Callable[[], datetime] | None = None,
) -> CleanResult:
    """Clean `table` for mapping {source column: canonical field}; pure unless `store` is given.

    Rows failing typing go to the dead-letter (original text kept); the H-02 threshold raises
    `TransformError`. Corrections are counted for accepted rows only.
    """
    types = {f.name: f.type for f in dictionary.fields}
    for target in mapping.values():
        if target not in types:
            raise ConfigError(f"mapping to unknown field '{target[:64]}'")
        if types[target] == "string" and target not in text_rules:
            raise ConfigError(f"no cleaning rule for string field '{target}'")
    index = {c: i for i, c in reversed(list(enumerate(table.columns)))}
    for c in mapping:
        if c not in index:
            raise ConfigError(f"{source}: source column '{c[:64]}' not in table")
    tokens = frozenset(t.casefold() for t in policy.null_tokens)
    plan = [(c, t, index[c], text_rules.get(t)) for c, t in mapping.items()]

    events: list[list[tuple[str, str]]] = []
    cleaned: list[list[str | None]] = []
    for raw in table.rows:
        ev: list[tuple[str, str]] = []
        out: list[str | None] = []
        for _, target, i, rule in plan:
            v = raw[i] if i < len(raw) else None
            if v is None:
                out.append(None)
                continue
            s, kinds = clean_text(v, rule, tokens)
            ev.extend((target, k) for k in kinds)
            out.append(s)
        events.append(ev)
        cleaned.append(out)

    ctable = RawTable([c for c, *_ in plan], cleaned)
    rules = bind_rules(transforms, {c: t for c, t, *_ in plan})
    ts = (now or (lambda: datetime.now(UTC)))()
    res = transform_table(ctable, rules, source=source, run_id=run_id, now=lambda: ts)
    rejects = [
        replace(r, raw_line=json.dumps(table.rows[r.line_number - 1], ensure_ascii=False))
        for r in res.rejects
    ]

    bad = {r.line_number for r in rejects}
    ok_numbers = [n for n in range(1, len(table.rows) + 1) if n not in bad]
    corrections = CorrectionLog()
    key_cols = [
        (j, t) for j, (_, t, _, _) in enumerate(plan)
        if policy.join_keys and text_rules.get(t) and text_rules[t].accent_key
    ]  # fmt: skip
    rows: list[list[Value]] = []
    for n, typed in zip(ok_numbers, res.rows, strict=True):
        for target, ekind in events[n - 1]:
            corrections.add(target, ekind, n)
        for j, (_, target, _, _) in enumerate(plan):
            kind = _converted(target, transforms[target], cleaned[n - 1][j], typed[j])
            if kind:
                corrections.add(target, kind, n)
        keys = [normalize(tv) if isinstance(tv := typed[j], str) else None for j, _ in key_cols]
        rows.append([*typed, *keys])
    columns = [*res.columns, *(f"{t}_key" for _, t in key_cols)]

    path = None
    if store is not None:
        path = write_rejects(rejects, source=source, day=day or ts.date(), store=store,
                             retry=RetryPolicy())  # fmt: skip
    st = res.stats
    by_kind = {f"{t}:{k}": n for (t, k), n in sorted(corrections.counts.items())}
    log.info(
        "clean",
        source=source,
        run_id=run_id,
        read=st.rows_read,
        rejected=st.rows_rejected,
        corrections=by_kind,
    )
    if st.rows_read and st.rows_rejected / st.rows_read >= policy.max_reject_rate:
        raise TransformError(
            f"{source}: {st.rows_rejected}/{st.rows_read} rows rejected by cleaning "
            f"(threshold {policy.max_reject_rate:.0%}); rejects stored at {path}"
        )
    unmapped = [c for c in table.columns if c not in mapping]
    return CleanResult(columns, rows, corrections, rejects, st, unmapped, path)
