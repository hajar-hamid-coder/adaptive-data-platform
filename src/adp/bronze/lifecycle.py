"""Bronze lifecycle: retention archiving by controlled MOVE (files are never rewritten).

Crash safety: for each batch, data files (parquet, rejects) are moved first and the manifest
LAST. The manifest is the completion marker (P1-05): while it is still at the source, the
batch is still "live" there, so a replay of `plan_archive` re-plans exactly the remaining files
and `write_bronze` replays remain no-ops. Once the manifest is archived, the batch is complete
under the archive prefix. Never the reverse (a manifest pointing to a missing parquet).
"""

from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from adp.bronze.store import ObjectStore
from adp.errors import ConfigError, IngestionError
from adp.logging import get_logger

log = get_logger(__name__)
SUFFIXES = (".parquet", ".manifest.json", ".rejects.jsonl", ".rejects.manifest.json")


class _Default(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    retention_days: int = Field(default=90, ge=1)
    archive_prefix: str = Field(default="_archive", pattern=r"^[A-Za-z0-9_-]+$")


class _SourceRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    retention_days: int = Field(ge=1)


class RetentionPolicy(BaseModel):
    """Default retention plus per-source overrides."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    default: _Default = _Default()
    sources: dict[str, _SourceRule] = {}

    def retention_for(self, source: str) -> int:
        rule = self.sources.get(source)
        return rule.retention_days if rule else self.default.retention_days


def load_retention(path: Path) -> RetentionPolicy:
    """Load and validate the retention YAML; errors name the file and field."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: cannot read YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a YAML mapping at top level")
    try:
        return RetentionPolicy.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(
            f"field '{'.'.join(str(p) for p in e['loc']) or '?'}': {e['msg']}" for e in exc.errors()
        )
        raise ConfigError(f"{path}: invalid retention config: {details}") from exc


@dataclass(frozen=True)
class Move:
    """One file to move; `batch` = '<ingestion_date=...>/<hash>' groups a batch's files."""

    source: str
    batch: str
    src: str
    dst: str


@dataclass(frozen=True)
class ArchiveResult:
    moved: int
    already_done: int


def _parse(key: str) -> tuple[str, date, str, str] | None:
    """(source, day, stem, filename) for a Bronze key, None for anything else."""
    parts = key.split("/")
    if len(parts) != 3 or not parts[1].startswith("ingestion_date=") or parts[2][0] == ".":
        return None
    if not parts[2].endswith(SUFFIXES):
        return None
    try:
        day = date.fromisoformat(parts[1].removeprefix("ingestion_date="))
    except ValueError:
        return None
    return parts[0], day, parts[2].split(".", 1)[0], parts[2]


def plan_archive(store: ObjectStore, policy: RetentionPolicy, today: date) -> list[Move]:
    """Pure plan from the listed state and the injected date; whole batches, manifest last."""
    arch = policy.default.archive_prefix
    entries = []
    for key in store.list():
        parsed = None if key.split("/", 1)[0] == arch else _parse(key)
        if parsed is None:
            continue
        source, day, stem, name = parsed
        if day < today - timedelta(days=policy.retention_for(source)):
            entries.append((source, day, stem, name.endswith(".manifest.json"), name, key))
    moves = []
    for source, day, stem, _, name, key in sorted(entries):
        batch = f"ingestion_date={day.isoformat()}/{stem}"
        moves.append(Move(source, batch, key, f"{arch}/{source}/ingestion_date={day}/{name}"))
    return moves


def apply_archive(store: ObjectStore, moves: list[Move]) -> ArchiveResult:
    """Execute a plan in order; replays are no-ops and nothing is ever lost."""
    moved = done = 0
    for m in moves:
        if not store.exists(m.src):
            if not store.exists(m.dst):
                raise IngestionError(f"archive: {m.src} and {m.dst} are both missing")
            done += 1
            continue
        store.move(m.src, m.dst)  # verifies identical content if dst already exists
        moved += 1
    log.info("bronze_archived", moved=moved, already_done=done)
    return ArchiveResult(moved, done)


def summarize(moves: list[Move]) -> dict[str, tuple[int, int]]:
    """Per source: (batches, files)."""
    files = Counter(m.source for m in moves)
    batches = Counter(s for s, _ in {(m.source, m.batch) for m in moves})
    return {s: (batches[s], files[s]) for s in sorted(files)}
