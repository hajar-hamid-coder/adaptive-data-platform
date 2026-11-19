"""Dagster asset `promote_to_gold_<source>`, downstream of `silver_<source>` (P4-07).

Thin wrapper over `adp.orchestration.promotion_gate`. Reads `silver_path` and `schema_version`
from the latest materialization of `silver_<source>`. A failed gate is NOT a Dagster failure:
the run stays green with metadata `status=quarantined`. Metadata: ids, counts, rule ids, paths.
"""

import os
from pathlib import Path
from typing import Any

from dagster import (
    AssetKey,
    AssetsDefinition,
    Failure,
    MaterializeResult,
    MetadataValue,
    OpExecutionContext,
    asset,
)

from adp.bronze.store import LocalStore, ObjectStore, S3Store
from adp.errors import AdpError
from adp.logging import bind_run_id
from adp.orchestration.promotion_gate import promote_batch
from adp.orchestration.silver_chain import load_run_dictionary
from adp.sources.spec import SourceSpec


def _store(env_root: str, default: str, bucket_env: str, bucket: str) -> ObjectStore:
    if os.environ.get("ADP_STORE", "local") == "s3":
        return S3Store(os.environ.get(bucket_env, bucket))
    return LocalStore(Path(os.environ.get(env_root, default)))


def build_gold_store() -> ObjectStore:
    """`gold_ready/` markers (ADP_GOLD_STORE_ROOT, default data/gold; s3: ADP_S3_GOLD_BUCKET)."""
    return _store("ADP_GOLD_STORE_ROOT", "data/gold", "ADP_S3_GOLD_BUCKET", "gold")


def build_quarantine_store() -> ObjectStore:
    """Quarantine (ADP_QUARANTINE_STORE_ROOT, default data/quarantine; s3: ADP_S3_QUARANTINE_BUCKET)."""  # noqa: E501
    return _store("ADP_QUARANTINE_STORE_ROOT", "data/quarantine", "ADP_S3_QUARANTINE_BUCKET",
                  "quarantine")  # fmt: skip


def make_promote_asset(spec: SourceSpec) -> AssetsDefinition:
    """Build `promote_to_gold_<source>`, downstream of `silver_<source>`."""
    silver = AssetKey(f"silver_{spec.name}")

    @asset(
        name=f"promote_to_gold_{spec.name}", group_name="gold", deps=[silver],
        required_resource_keys={"silver_store", "gold_store", "quarantine_store",
                                "schema_registry"},
    )  # fmt: skip
    def _promote(context: OpExecutionContext) -> MaterializeResult[None]:
        run_id = bind_run_id(context.run_id)
        event = context.instance.get_latest_materialization_event(silver)
        meta = event.asset_materialization.metadata if event and event.asset_materialization else {}
        path = str(meta["silver_path"].value) if "silver_path" in meta else ""
        version = meta["schema_version"].value if "schema_version" in meta else None
        if not path:
            raise Failure(description=f"{spec.name}: Silver gave no batch path",
                          allow_retries=False)  # fmt: skip
        try:
            res = promote_batch(
                source=spec.name, silver_path=path, run_id=run_id,
                dictionary=load_run_dictionary(),
                silver_store=context.resources.silver_store,
                gold_store=context.resources.gold_store,
                quarantine_store=context.resources.quarantine_store,
                report_dir=Path(os.environ.get("ADP_REPORT_DIR", "data/reports")),
                engine=context.resources.schema_registry.engine(),
                schema_version=version if type(version) is int else None,
            )  # fmt: skip
        except AdpError as exc:
            raise Failure(description=f"{spec.name}: promotion failed ({type(exc).__name__}): "
                          f"{exc}", allow_retries=False) from exc  # fmt: skip
        md: dict[str, Any] = {
            "status": res.status,
            "reasons": MetadataValue.json(res.reasons),
            "batch_id": res.batch_id,
            "score": res.score if res.score is not None else "n/a",
            "rows": res.rows,
            "rows_quarantined": res.rows_quarantined,
            "gold_path": MetadataValue.text(res.gold_path),
            "skipped": res.skipped,
            "report_json": MetadataValue.path(res.report_json),
            "report_html": MetadataValue.path(res.report_html),
        }
        return MaterializeResult(metadata=md)

    return _promote


def make_promote_assets(registry: dict[str, SourceSpec]) -> list[AssetsDefinition]:
    """One promotion asset per `file` source (same set as Silver)."""
    return [make_promote_asset(s) for s in registry.values() if s.type == "file"]
