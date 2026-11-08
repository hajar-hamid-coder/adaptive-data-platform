"""Dagster ingestion layer: one Bronze asset per `file` source, plus a landing-dir sensor.

Thin wrapper: all business logic stays in `adp.bronze` / `adp.connectors`.
Not partitioned (H-08 note): ingestion_date is the run day and replays are idempotent via the
batch hash, so a daily partition would add no safety; the CLI also needs no --partition.
`api` / `database` sources get no asset here (they need network); they are logged and skipped.
"""

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

from dagster import (
    AssetKey,
    AssetsDefinition,
    AssetSelection,
    Config,
    MaterializeResult,
    MetadataValue,
    OpExecutionContext,
    RunConfig,
    RunRequest,
    SensorDefinition,
    SensorEvaluationContext,
    SkipReason,
    asset,
    sensor,
)

from adp.bronze.deadletter import ingest_with_deadletter
from adp.bronze.store import LocalStore, ObjectStore, S3Store
from adp.logging import bind_run_id, get_logger
from adp.sources.spec import SourceSpec

log = get_logger(__name__)
DEFAULT_LANDING = "data/landing"


class BronzeConfig(Config):
    """Run config: `path` overrides the source URI (set by the landing sensor)."""

    path: str = ""


def landing_dir() -> Path:
    """Landing directory (env ADP_LANDING_DIR, relative to cwd by default)."""
    return Path(os.environ.get("ADP_LANDING_DIR", DEFAULT_LANDING))


def build_store() -> ObjectStore:
    """ADP_STORE=local (default, root ADP_STORE_ROOT) or s3 (bucket ADP_S3_BUCKET)."""
    if os.environ.get("ADP_STORE", "local") == "s3":
        return S3Store(os.environ.get("ADP_S3_BUCKET", "bronze"))
    return LocalStore(Path(os.environ.get("ADP_STORE_ROOT", "data/bronze")))


def make_bronze_asset(spec: SourceSpec) -> AssetsDefinition:
    """Build `bronze_<source>` for a file source."""

    @asset(name=f"bronze_{spec.name}", group_name="bronze", required_resource_keys={"store"})
    def _bronze(context: OpExecutionContext, config: BronzeConfig) -> MaterializeResult[None]:
        run_id = bind_run_id(context.run_id)
        s = context.resources.store
        summary = ingest_with_deadletter(
            Path(config.path or spec.uri), source=spec.name, run_id=run_id, store=s,
            ingestion_date=datetime.now(UTC).date(),
        )  # fmt: skip
        mf = summary.bronze
        return MaterializeResult(
            metadata={
                "rows_ok": summary.rows_ok,
                "rows_rejected": summary.rows_rejected,
                "batch_hash": MetadataValue.text(mf.batch_hash if mf else ""),
                "parquet_path": MetadataValue.text(mf.parquet_path if mf else ""),
                "rejects_path": MetadataValue.text(summary.rejects_path or ""),
                "skipped": bool(mf and mf.skipped),
            }
        )

    return _bronze


def make_bronze_assets(registry: dict[str, SourceSpec]) -> list[AssetsDefinition]:
    """One asset per `file` source; other types are logged and left out."""
    assets = []
    for spec in registry.values():
        if spec.type == "file":
            assets.append(make_bronze_asset(spec))
        else:
            log.debug("source_not_orchestrated", source=spec.name, type=spec.type)
    return assets


def make_landing_sensor(registry: dict[str, SourceSpec]) -> SensorDefinition:
    """Watch `<landing>/<source>/*`; each new/changed file triggers that source's asset."""
    names = [n for n, s in registry.items() if s.type == "file"]

    @sensor(name="landing_files_sensor", asset_selection=AssetSelection.all(),
            minimum_interval_seconds=30)  # fmt: skip
    def _sensor(context: SensorEvaluationContext) -> RunRequest | SkipReason:
        root = landing_dir()
        for name in names:
            for f in sorted((root / name).glob("*")) if (root / name).is_dir() else []:
                if not f.is_file() or f.name.startswith("."):
                    continue
                st = f.stat()
                key = hashlib.sha256(f"{f.resolve()}:{st.st_mtime_ns}:{st.st_size}".encode())
                run_key = key.hexdigest()[:24]
                if context.cursor and run_key in context.cursor.split(","):
                    continue
                context.update_cursor(f"{context.cursor or ''},{run_key}".strip(","))
                return RunRequest(
                    run_key=run_key,
                    asset_selection=[AssetKey(f"bronze_{name}")],
                    run_config=RunConfig(ops={f"bronze_{name}": BronzeConfig(path=str(f))}),
                )
        return SkipReason(f"no new file in {root}")

    return _sensor
