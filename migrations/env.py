"""Alembic environment for the ADP metadata database (online mode only).

Only the registry tables are managed: other tables of the same database (e.g. `adp_watermarks`,
created on demand by the SQL connector) are ignored by autogenerate.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from adp.config import get_settings
from adp.schema.registry import metadata

config = context.config
if config.config_file_name is not None and not config.attributes.get("url"):
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _include_name(name: str | None, type_: str, parent_names: object) -> bool:
    return type_ != "table" or name in metadata.tables


url = config.attributes.get("url") or get_settings().postgres_url()
engine = create_engine(url)
try:
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=metadata, include_name=_include_name
        )
        with context.begin_transaction():
            context.run_migrations()
finally:
    engine.dispose()
