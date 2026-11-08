"""Alembic migrations: no drift between the revision and the registry model; upgrade on Postgres."""

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, create_engine, inspect

from adp.connectors.files import RawTable
from adp.schema.profile import SchemaFingerprint, profile_table
from adp.schema.registry import SchemaRegistry, metadata

ROOT = Path(__file__).parents[2]


def fp(columns: list[str]) -> SchemaFingerprint:
    return profile_table(RawTable(columns=columns, rows=[["1"] * len(columns)]))


def _upgrade(url: str) -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["url"] = url
    command.upgrade(cfg, "head")


def _assert_no_drift(engine: Engine) -> None:
    with engine.connect() as c:
        diff = compare_metadata(MigrationContext.configure(c), metadata)
    assert diff == []


def test_revision_matches_model_sqlite(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'm.db'}"
    _upgrade(url)
    eng = create_engine(url)
    assert {"schema_version", "schema_change"} <= set(inspect(eng).get_table_names())
    _assert_no_drift(eng)
    _upgrade(url)  # re-running is a no-op
    assert SchemaRegistry(eng).register("hcp", fp(["a"])).version == 1
    eng.dispose()


@pytest.mark.slow
def test_upgrade_postgres(pg_engine: Engine) -> None:
    _upgrade(pg_engine.url.render_as_string(hide_password=False))
    tables = set(inspect(pg_engine).get_table_names())
    assert tables == {"schema_version", "schema_change", "alembic_version"}
    _assert_no_drift(pg_engine)
    reg = SchemaRegistry(pg_engine)
    assert reg.register("hcp", fp(["a"])) == reg.register("hcp", fp(["a"]))
