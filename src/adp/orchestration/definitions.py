"""Assemble Dagster Definitions (lazy: no I/O at import time)."""

import os
from pathlib import Path

from dagster import Definitions, definitions

from adp.orchestration.ingestion import build_store, make_bronze_assets, make_landing_sensor
from adp.sources.spec import load_registry


def build_definitions(sources_dir: Path | None = None) -> Definitions:
    """Definitions from the YAML source registry (env ADP_SOURCES_DIR, default config/sources)."""
    registry = load_registry(
        sources_dir or Path(os.environ.get("ADP_SOURCES_DIR", "config/sources"))
    )
    return Definitions(
        assets=make_bronze_assets(registry),
        sensors=[make_landing_sensor(registry)],
        resources={"store": build_store()},
    )


@definitions
def defs() -> Definitions:
    """Entry point discovered by `dagster ... -m adp.orchestration`."""
    return build_definitions()
