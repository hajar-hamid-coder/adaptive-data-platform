"""Immutable Bronze writer: Parquet + JSON manifest, idempotent per logical batch."""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb

from adp.bronze.store import ObjectStore
from adp.connectors.files import RawTable
from adp.errors import IngestionError
from adp.logging import get_logger

TECH_COLUMNS = ("_run_id", "_ingested_at", "_source", "_row_hash")
log = get_logger(__name__)


@dataclass(frozen=True)
class BronzeManifest:
    """Describes one written batch; `skipped` is runtime-only (not persisted)."""

    source: str
    run_id: str
    batch_hash: str
    row_count: int
    columns: list[str]
    schema: dict[str, str]
    parquet_path: str
    ingested_at: str
    size_bytes: int
    skipped: bool = False


def row_hash(columns: list[str], row: list[str | None]) -> str:
    """Stable sha256 of a row: JSON-encoded (column, value) pairs, unambiguous."""
    return hashlib.sha256(
        json.dumps(list(zip(columns, row, strict=True)), ensure_ascii=False).encode()
    ).hexdigest()


def batch_hash(columns: list[str], row_hashes: list[str]) -> str:
    """Hash of the logical content (columns + sorted row hashes), independent of run metadata."""
    payload = json.dumps([columns, sorted(row_hashes)], ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _to_parquet(
    table: RawTable, tech: list[list[str]], dest: Path
) -> dict[str, str]:  # fmt: skip
    cols = [*table.columns, *TECH_COLUMNS]
    ddl = ", ".join(f'"{c.replace(chr(34), chr(34) * 2)}" VARCHAR' for c in cols)
    con = duckdb.connect()
    try:
        con.execute(f"CREATE TABLE t ({ddl})")
        marks = ",".join("?" * len(cols))
        con.executemany(
            f"INSERT INTO t VALUES ({marks})",
            [[*r, *t] for r, t in zip(table.rows, tech, strict=True)],
        )
        con.execute(f"COPY t TO '{dest}' (FORMAT PARQUET)")
        return {r[0]: r[1] for r in con.execute("DESCRIBE t").fetchall()}
    finally:
        con.close()


def write_bronze(
    table: RawTable,
    *,
    source: str,
    run_id: str,
    store: ObjectStore,
    ingestion_date: date | None = None,
    now: Callable[[], datetime] | None = None,
) -> BronzeManifest:
    """Write a batch to Bronze once; replaying the same logical batch is a no-op."""
    clash = set(table.columns) & set(TECH_COLUMNS)
    if clash or len(set(table.columns)) != len(table.columns):
        raise IngestionError(f"{source}: duplicate or reserved column names {sorted(clash)}")
    ts = (now or (lambda: datetime.now(UTC)))()
    day = ingestion_date or ts.date()
    hashes = [row_hash(table.columns, r) for r in table.rows]
    bh = batch_hash(table.columns, hashes)
    prefix = f"{source}/ingestion_date={day.isoformat()}/{bh}"
    pq_key, mf_key = f"{prefix}.parquet", f"{prefix}.manifest.json"

    if store.exists(mf_key):
        log.info("bronze_skip", source=source, run_id=run_id, batch_hash=bh)
        return _skipped(store, mf_key)

    ingested_at = ts.astimezone(UTC).isoformat()
    tech = [[run_id, ingested_at, source, h] for h in hashes]
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "batch.parquet"
        schema = _to_parquet(table, tech, local)
        data = local.read_bytes()
    if not store.put_if_absent(pq_key, data):  # incomplete earlier attempt: keep its parquet
        data = store.get(pq_key)
    manifest = BronzeManifest(
        source=source, run_id=run_id, batch_hash=bh, row_count=len(table.rows),
        columns=table.columns, schema=schema, parquet_path=pq_key,
        ingested_at=ingested_at, size_bytes=len(data),
    )  # fmt: skip
    body = {k: v for k, v in asdict(manifest).items() if k != "skipped"}
    if not store.put_if_absent(mf_key, json.dumps(body, indent=2).encode()):
        return _skipped(store, mf_key)
    log.info("bronze_written", source=source, run_id=run_id, batch_hash=bh, rows=manifest.row_count)
    return manifest


def _skipped(store: ObjectStore, mf_key: str) -> BronzeManifest:
    return BronzeManifest(**json.loads(store.get(mf_key)), skipped=True)
