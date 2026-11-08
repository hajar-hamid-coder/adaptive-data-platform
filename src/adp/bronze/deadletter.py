"""Dead-letter: rejected rows are stored (never dropped) with their reason."""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal, TypeVar

from adp.bronze.store import ObjectStore
from adp.bronze.writer import BronzeManifest, write_bronze
from adp.connectors.files import BadRow, read_file_with_rejects
from adp.errors import IngestionError
from adp.logging import get_logger

Reason = Literal["wrong_field_count", "invalid_json", "not_an_object"]
T = TypeVar("T")
log = get_logger(__name__)


@dataclass(frozen=True)
class Reject:
    """One rejected record; `raw_line` is the original text, untouched."""

    source: str
    run_id: str
    reason: str
    raw_line: str
    line_number: int
    rejected_at: str


@dataclass(frozen=True)
class RetryPolicy:
    """Retry transient store-write errors (never corrupted data); H-02: 3 attempts."""

    max_attempts: int = 3
    backoff: float = 0.5
    sleep: Callable[[float], None] = time.sleep
    retry_on: tuple[type[BaseException], ...] = (OSError, IngestionError)

    def call(self, fn: Callable[[], T]) -> T:
        for attempt in range(1, self.max_attempts + 1):
            try:
                return fn()
            except self.retry_on as e:
                if attempt == self.max_attempts:
                    raise
                log.warning("store_retry", attempt=attempt, error=type(e).__name__)
                self.sleep(self.backoff * 2 ** (attempt - 1))
        raise AssertionError("unreachable")  # pragma: no cover


@dataclass(frozen=True)
class IngestSummary:
    rows_ok: int
    rows_rejected: int
    by_reason: dict[str, int] = field(default_factory=dict)
    bronze: BronzeManifest | None = None
    rejects_path: str | None = None


def write_rejects(
    rejects: list[Reject], *, source: str, day: date, store: ObjectStore, retry: RetryPolicy
) -> str | None:
    """Write rejects (JSON lines) + counts manifest once; same rejects replayed = no-op."""
    if not rejects:
        return None
    ordered = sorted(rejects, key=lambda r: (r.line_number, r.raw_line))
    ident = [[r.line_number, r.reason, r.raw_line] for r in ordered]
    rh = hashlib.sha256(json.dumps([source, ident], ensure_ascii=False).encode()).hexdigest()
    prefix = f"{source}/ingestion_date={day.isoformat()}/{rh}.rejects"
    body = "".join(json.dumps(asdict(r), ensure_ascii=False) + "\n" for r in ordered)
    counts = dict(Counter(r.reason for r in rejects))
    manifest = {"source": source, "reject_hash": rh, "count": len(rejects), "by_reason": counts}
    retry.call(lambda: store.put_if_absent(f"{prefix}.jsonl", body.encode()))
    retry.call(
        lambda: store.put_if_absent(f"{prefix}.manifest.json", json.dumps(manifest).encode())
    )
    return f"{prefix}.jsonl"


def ingest_with_deadletter(
    path: Path,
    *,
    source: str,
    run_id: str,
    store: ObjectStore,
    ingestion_date: date | None = None,
    now: Callable[[], datetime] | None = None,
    max_reject_rate: float = 0.20,
    retry: RetryPolicy | None = None,
) -> IngestSummary:
    """Read a file, send bad rows to dead-letter, valid rows to Bronze; nothing is lost."""
    retry = retry or RetryPolicy()
    ts = (now or (lambda: datetime.now(UTC)))()
    day = ingestion_date or ts.date()
    table, bad = read_file_with_rejects(path)
    rejects = [_to_reject(b, source, run_id, ts) for b in bad]
    read = int(table.meta["records_read"])
    if len(table.rows) + len(rejects) != read:
        raise IngestionError(f"{source}: row accounting mismatch (read {read})")
    rejects_path = write_rejects(rejects, source=source, day=day, store=store, retry=retry)
    by_reason = dict(Counter(r.reason for r in rejects))
    log.info("deadletter", source=source, run_id=run_id, read=read, rejected=len(rejects),
             by_reason=by_reason)  # fmt: skip
    if read and len(rejects) / read >= max_reject_rate:
        raise IngestionError(
            f"{source}: {len(rejects)}/{read} rows rejected (threshold {max_reject_rate:.0%}); "
            f"rejects stored at {rejects_path}"
        )
    manifest = retry.call(
        lambda: write_bronze(
            table, source=source, run_id=run_id, store=store, ingestion_date=day, now=lambda: ts
        )
    )
    return IngestSummary(len(table.rows), len(rejects), by_reason, manifest, rejects_path)


def _to_reject(b: BadRow, source: str, run_id: str, ts: datetime) -> Reject:
    return Reject(source, run_id, b.reason, b.raw, b.line_number, ts.astimezone(UTC).isoformat())
