"""Safe continuous improvement: human-approved mappings become learned synonyms.

A source column name is UNTRUSTED data: a poisoned glossary would silently redirect future
mappings. Hence, strictly:
- Only HUMAN `approve` decisions of the append-only `mapping_decision_log` teach (actor not empty,
  not 'system'). auto_apply, pending, rejected and LLM/fuzzy proposals never do. For one column
  only its LATEST decision counts: a later `revert` (or `reject`) therefore un-learns it.
- The target must exist in the glossary; the normalized synonym is 3..64 chars, printable, not
  made only of generic tokens, not already a term of ANOTHER field (collision: refused, never
  overwritten); the same name approved towards two targets is a conflict: nothing is learned.
- At most `MAX_PER_FIELD` learned synonyms per field.
- Storage: `config/semantic/canonical.yaml` (curated, versioned) is NEVER written. Learned entries
  live in a separate file. Default `data/semantic/learned_synonyms.yaml`: runtime state, git-ignored
  like all of `data/` (a curated YAML in git would let a data-driven loop dirty the repo and
  bypass code review; the audit trail is the decision journal, whose ids each entry cites).
  The file only grows: `entries` are appended, a revoked entry is listed by decision id in
  `revoked` (never edited or deleted). Written atomically with `yaml.safe_dump`.
- Loading revalidates every entry with the same rules and never trusts the file: invalid,
  hostile or conflicting entries are ignored (counted, logged), the curated glossary always wins,
  and a broken file never stops the platform (worst case: curated glossary only).
The rule cascade then resolves a learned name with method `synonym` (0.95): no LLM call.
No data value is involved, only column names (metadata).
"""

import os
import tempfile
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from adp.logging import get_logger
from adp.mapping.decision import SYSTEM, LogEntry
from adp.mapping.dictionary import SemanticDictionary, load_dictionary, normalize
from adp.mapping.rules import GENERIC_TOKENS

log = get_logger(__name__)

DEFAULT_LEARNED_PATH = Path("data/semantic/learned_synonyms.yaml")
MIN_LEN, MAX_LEN = 3, 64
MAX_PER_FIELD = 50
_FORMAT = 1


class LearnedSynonym(BaseModel):
    """One learned entry as stored (strict: unknown keys are refused)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    synonym: str = Field(min_length=1, max_length=MAX_LEN)  # already normalized
    target: str = Field(min_length=1, max_length=255)
    approved_by: str = Field(min_length=1, max_length=128)
    approved_at: str = Field(min_length=1, max_length=64)
    decision_id: int
    source_name: str = Field(min_length=1, max_length=255)


class Refusal(BaseModel):
    """An approved mapping that was not learned, with a short reason code."""

    model_config = ConfigDict(frozen=True)

    source_name: str
    source_col: str
    reason: str  # unknown_target | invalid_synonym | collision | conflict | cap


class LearnResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    learned: list[LearnedSynonym]
    refused: list[Refusal]
    already_known: int = 0


class MergeReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    applied: int
    ignored: int
    revoked: int = 0


def _is_human(actor: str) -> bool:
    a = (actor or "").strip().lower()
    return bool(a) and a != SYSTEM


def check_synonym(raw: str) -> str | None:
    """Normalized synonym, or None when unsafe/unusable (length, control chars, generic only)."""
    if not raw or not raw.isprintable():
        return None
    norm = normalize(raw)
    if not MIN_LEN <= len(norm) <= MAX_LEN or set(norm.split("_")) <= GENERIC_TOKENS:
        return None
    return norm


def compute_learned(
    rows: Sequence[LogEntry], dictionary: SemanticDictionary, *, max_per_field: int = MAX_PER_FIELD
) -> list[LearnedSynonym]:
    """Pure: synonyms to learn from journal rows (see `analyze` for refusals)."""
    return analyze(rows, dictionary, max_per_field=max_per_field).learned


def analyze(
    rows: Sequence[LogEntry], dictionary: SemanticDictionary, *, max_per_field: int = MAX_PER_FIELD
) -> LearnResult:
    """Pure: apply the safety rules to the journal. `dictionary` must be the CURATED one."""
    fields = {f.name for f in dictionary.fields}
    latest: dict[tuple[str, str], LogEntry] = {}
    for e in sorted(rows, key=lambda r: r.id):
        if e.action in ("auto_apply", "approve", "reject", "revert"):
            latest[(e.source_name, e.source_col)] = e
    approved = [e for e in latest.values() if e.action == "approve" and _is_human(e.actor)]
    refused: list[Refusal] = []
    known = 0

    def refuse(e: LogEntry, reason: str) -> None:
        refused.append(Refusal(source_name=e.source_name, source_col=e.source_col, reason=reason))

    by_syn: dict[str, list[tuple[str, LogEntry]]] = defaultdict(list)
    for e in sorted(approved, key=lambda r: r.id):
        norm = check_synonym(e.source_col)
        if e.target is None or e.target not in fields:
            refuse(e, "unknown_target")
        elif norm is None:
            refuse(e, "invalid_synonym")
        else:
            by_syn[norm].append((norm, e))
    chosen: list[LearnedSynonym] = []
    per_field: dict[str, int] = defaultdict(int)
    for norm, items in sorted(by_syn.items(), key=lambda kv: kv[1][0][1].id):
        entries = [e for _, e in items]
        if len({e.target for e in entries}) > 1:
            for e in entries:
                refuse(e, "conflict")
            continue
        first = entries[0]
        target = first.target or ""
        owner = dictionary.lookup(norm)
        if owner is not None and owner.name != target:
            for e in entries:
                refuse(e, "collision")
        elif owner is not None:
            known += 1  # already a term of that very field: nothing to learn
        elif per_field[target] >= max_per_field:
            for e in entries:
                refuse(e, "cap")
        else:
            per_field[target] += 1
            chosen.append(
                LearnedSynonym(
                    synonym=norm, target=target, approved_by=first.actor.strip(),
                    approved_at=first.decided_at.isoformat(), decision_id=first.id,
                    source_name=first.source_name,
                )
            )  # fmt: skip
    chosen.sort(key=lambda s: s.decision_id)
    return LearnResult(learned=chosen, refused=refused, already_known=known)


# --- storage -------------------------------------------------------------------------------
def _read_raw(path: Path) -> dict[str, Any]:
    """Raw content of the learned file ({} if absent or unreadable: never fatal)."""
    if not path.is_file():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        log.warning("learned_file_unreadable", path=str(path))
        return {}
    if not isinstance(raw, dict):
        log.warning("learned_file_unreadable", path=str(path))
        return {}
    return raw


def _parse(raw: dict[str, Any]) -> tuple[list[LearnedSynonym], set[int], int]:
    """Valid entries, revoked decision ids, and the count of malformed items dropped."""
    bad = 0
    entries: list[LearnedSynonym] = []
    items = raw.get("entries")
    for item in items if isinstance(items, list) else []:
        try:
            entries.append(LearnedSynonym.model_validate(item))
        except ValidationError:
            bad += 1
    revoked_raw = raw.get("revoked")
    revoked = {i for i in (revoked_raw if isinstance(revoked_raw, list) else [])
               if isinstance(i, int) and not isinstance(i, bool)}  # fmt: skip
    return entries, revoked, bad


def merge_learned(
    curated: SemanticDictionary,
    entries: Sequence[LearnedSynonym],
    *,
    max_per_field: int = MAX_PER_FIELD,
) -> tuple[SemanticDictionary, MergeReport]:
    """Curated dictionary + revalidated learned entries; invalid ones are ignored (counted)."""
    fields = {f.name for f in curated.fields}
    added: dict[str, list[str]] = defaultdict(list)
    taken: dict[str, str] = {}
    ignored = 0
    for e in entries:
        owner = curated.lookup(e.synonym)
        ok = (
            e.target in fields
            and check_synonym(e.synonym) == e.synonym
            and owner is None
            and taken.get(e.synonym, e.target) == e.target
            and len(added[e.target]) < max_per_field
        )
        if not ok:
            ignored += 1
            continue
        if e.synonym not in taken:
            taken[e.synonym] = e.target
            added[e.target].append(e.synonym)
    new_fields = tuple(
        f.model_copy(update={"synonyms": (*f.synonyms, *added[f.name])}) if added[f.name] else f
        for f in curated.fields
    )
    merged = SemanticDictionary(fields=new_fields)  # prechecked: cannot collide
    return merged, MergeReport(applied=sum(len(v) for v in added.values()), ignored=ignored)


def load_dictionary_with_learned(canonical_path: Path, learned_path: Path) -> SemanticDictionary:
    """Curated dictionary plus valid learned synonyms (curated wins; never fatal on bad entries)."""
    curated = load_dictionary(canonical_path)
    entries, revoked, bad = _parse(_read_raw(learned_path))
    active = [e for e in entries if e.decision_id not in revoked]
    merged, rep = merge_learned(curated, active)
    ignored = rep.ignored + bad
    if ignored:
        log.warning("learned_entries_ignored", count=ignored, applied=rep.applied)
    log.info("learned_synonyms_loaded", applied=rep.applied, ignored=ignored)
    return merged


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def update_learned_file(
    canonical_path: Path, learned_path: Path, rows: Sequence[LogEntry]
) -> LearnResult:
    """Append newly approved synonyms to the learned file and revoke the withdrawn ones.

    Idempotent: replaying the same journal changes nothing (the file is not even rewritten).
    The curated dictionary is only read.
    """
    curated = load_dictionary(canonical_path)
    result = analyze(rows, curated)
    entries, revoked, _ = _parse(_read_raw(learned_path))
    active = {e.decision_id: e for e in entries if e.decision_id not in revoked}
    wanted = {s.decision_id: s for s in result.learned}
    kept = {e.synonym for i, e in active.items() if i in wanted}
    new = [s for i, s in wanted.items() if i not in active and s.synonym not in kept]
    retire = sorted(i for i in active if i not in wanted)
    if new or retire:
        body = {
            "version": _FORMAT,
            "entries": [e.model_dump(mode="json") for e in (*entries, *new)],
            "revoked": sorted(revoked | set(retire)),
        }
        _atomic_write(learned_path, yaml.safe_dump(body, allow_unicode=True, sort_keys=False))
    log.info("learned_synonyms_updated", added=len(new), revoked=len(retire),
             refused=len(result.refused))  # fmt: skip
    return LearnResult(learned=new, refused=result.refused, already_known=result.already_known)


def render_report(result: LearnResult) -> str:
    """French summary of what was learned or refused (column names only, no row content)."""
    lines = [
        f"Apprentissage : {len(result.learned)} synonyme(s) appris, "
        f"{len(result.refused)} refusé(s), {result.already_known} déjà connu(s)."
    ]
    lines += [
        f"  + {s.synonym} -> {s.target} (approuvé par {s.approved_by})" for s in result.learned
    ]
    reasons = {
        "unknown_target": "cible absente du glossaire",
        "invalid_synonym": "nom inutilisable (longueur, caractères, termes trop génériques)",
        "collision": "déjà synonyme d'un autre champ",
        "conflict": "approuvé vers deux cibles différentes : revue nécessaire",
        "cap": "plafond de synonymes appris atteint pour ce champ",
    }
    lines += [
        f"  - {r.source_name}.{r.source_col!r:.80} : {reasons.get(r.reason, r.reason)}"
        for r in result.refused
    ]
    return "\n".join(lines)
