"""Assemble Dagster Definitions (lazy: no I/O at import time)."""

import os
from pathlib import Path

from dagster import Definitions, definitions

from adp.orchestration.ingestion import build_store, make_bronze_assets, make_landing_sensor
from adp.orchestration.maintenance import make_maintenance
from adp.orchestration.promote_asset import (
    build_gold_store,
    build_quarantine_store,
    make_promote_assets,
)
from adp.orchestration.replay import make_replay_sensor
from adp.orchestration.schema_gate import SchemaRegistryResource, make_schema_check_assets
from adp.orchestration.silver_chain import build_silver_store, make_silver_assets
from adp.sources.spec import load_registry


def build_definitions(sources_dir: Path | None = None) -> Definitions:
    """Definitions from the YAML source registry (env ADP_SOURCES_DIR, default config/sources)."""
    registry = load_registry(
        sources_dir or Path(os.environ.get("ADP_SOURCES_DIR", "config/sources"))
    )
    jobs, schedules = make_maintenance()
    return Definitions(
        assets=[
            *make_bronze_assets(registry),
            *make_schema_check_assets(registry),
            *make_silver_assets(registry),
            *make_promote_assets(registry),
        ],
        sensors=[make_landing_sensor(registry), make_replay_sensor(registry)],
        jobs=jobs,
        schedules=schedules,
        resources={
            "store": build_store(),
            "silver_store": build_silver_store(),
            "gold_store": build_gold_store(),
            "quarantine_store": build_quarantine_store(),
            "schema_registry": SchemaRegistryResource(),
        },
    )


@definitions
def defs() -> Definitions:
    """Entry point discovered by `dagster ... -m adp.orchestration`."""
    return build_definitions()
