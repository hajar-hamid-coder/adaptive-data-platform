"""Shared fixtures for schema tests."""

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text

from adp.config import get_settings


@pytest.fixture
def pg_engine() -> Iterator[Engine]:
    """Throw-away database `adp_test_<uuid>` on the dev Postgres, dropped afterwards."""
    s = get_settings()
    admin = create_engine(s.postgres_url(), isolation_level="AUTOCOMMIT")
    name = f"adp_test_{uuid.uuid4().hex[:12]}"
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception:
        admin.dispose()
        pytest.skip("Postgres de dev injoignable")
    eng = create_engine(s.postgres_url(database=name))
    yield eng
    eng.dispose()
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()
