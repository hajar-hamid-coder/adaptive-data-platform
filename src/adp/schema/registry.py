"""Versioned schema registry stored in the metadata database (PostgreSQL; SQLite in unit tests).

Design decisions (ADR 0003, H-04):
- Portable SQLAlchemy Core tables, no vendor type; the DDL is owned by Alembic (`migrations/`),
  never created on the fly here. `metadata` is exported for Alembic and for tests.
- A version is identified by the *structural* fingerprint of `SchemaFingerprint` (ordered
  name, type, nullability). `register` is idempotent: a known fingerprint for the source returns
  the existing version without writing. Otherwise version = max + 1 (1 for the first one).
- Back to an older schema (v1 -> v2 -> v1): the existing version 1 is returned, nothing is
  written; `latest` stays the highest version (2). The registry is a catalog of distinct
  schemas; which one a given batch uses is recorded with the batch, not here.
- Concurrency: UNIQUE(source_name, fingerprint) and UNIQUE(source_name, version) arbitrate;
  the loser of a race gets an IntegrityError, rolls back and re-reads (bounded retries).
- Semver is computed once at registration against the highest version by `classify_bump`
  (minimal rule, to be refined/reused by the P2-03 diff). `schema_change` is filled by P2-03;
  `record_changes` replaces the rows of a (from, to) pair so a replay never duplicates.
"""

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    Column,
    DateTime,
    Engine,
    Integer,
    MetaData,
    Row,
    String,
    Table,
    Text,
    UniqueConstraint,
    delete,
    insert,
    select,
)
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from adp.errors import SchemaError
from adp.logging import get_logger
from adp.schema.profile import SchemaFingerprint

log = get_logger(__name__)

Bump = Literal["major", "minor", "patch"]

_SOURCE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,254}$")  # same rule as SourceSpec.name
_MAX_RETRIES = 5

metadata = MetaData()

schema_version = Table(
    "schema_version",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source_name", String(255), nullable=False),
    Column("version", Integer, nullable=False),
    Column("semver", String(32), nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("columns_json", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("source_name", "version", name="uq_schema_version_source_version"),
    UniqueConstraint("source_name", "fingerprint", name="uq_schema_version_source_fingerprint"),
)

schema_change = Table(
    "schema_change",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source_name", String(255), nullable=False, index=True),
    Column("from_version", Integer, nullable=True),
    Column("to_version", Integer, nullable=False),
    Column("change_type", String(64), nullable=False),
    Column("column_name", String(255), nullable=True),
    Column("detail_json", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


class SchemaColumn(BaseModel):
    """Structural part of a column: what the fingerprint covers."""

    model_config = ConfigDict(frozen=True)

    name: str
    type: str
    nullable: bool


class SchemaVersion(BaseModel):
    """One registered schema of a source."""

    model_config = ConfigDict(frozen=True)

    source_name: str
    version: int
    semver: str
    fingerprint: str
    columns: tuple[SchemaColumn, ...]
    created_at: datetime


class SchemaChange(BaseModel):
    """One change between two versions (vocabulary defined by the P2-03 diff)."""

    model_config = ConfigDict(frozen=True)

    change_type: str = Field(min_length=1, max_length=64)
    column_name: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class ChangeRecord(BaseModel):
    """A stored `SchemaChange` with its version pair."""

    model_config = ConfigDict(frozen=True)

    source_name: str
    from_version: int | None
    to_version: int
    change: SchemaChange
    created_at: datetime


def classify_bump(prev: Sequence[SchemaColumn], new: Sequence[SchemaColumn]) -> Bump:
    """major: a column dropped (a rename counts as drop + add) or retyped; minor: only
    additions; patch: anything else (nullability, column order)."""
    before = {c.name: c.type for c in prev}
    after = {c.name: c.type for c in new}
    if any(name not in after or after[name] != typ for name, typ in before.items()):
        return "major"
    return "minor" if after.keys() - before.keys() else "patch"


def _bump(semver: str, kind: Bump) -> str:
    major, minor, patch = (int(p) for p in semver.split("."))
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def _check_source(name: str) -> str:
    if not isinstance(name, str) or not _SOURCE_NAME.fullmatch(name):
        raise SchemaError(f"invalid source name: {name[:64]!r}")
    return name


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # SQLite drops the time zone


def _load_columns(raw: str) -> tuple[SchemaColumn, ...]:
    return tuple(SchemaColumn.model_validate(c) for c in json.loads(raw))


def _to_version(row: Row[Any]) -> SchemaVersion:
    return SchemaVersion(
        source_name=row.source_name,
        version=row.version,
        semver=row.semver,
        fingerprint=row.fingerprint,
        columns=_load_columns(row.columns_json),
        created_at=_utc(row.created_at),
    )


class SchemaRegistry:
    """Register and read schema versions per source. Raises SchemaError on any failure."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def register(self, source_name: str, fingerprint: SchemaFingerprint) -> SchemaVersion:
        """Return the version of this structure, creating it if new (idempotent)."""
        _check_source(source_name)
        if not fingerprint.columns:
            raise SchemaError(f"{source_name}: schema has no column")
        cols = tuple(
            SchemaColumn(name=c.name, type=c.inferred_type, nullable=c.nullable)
            for c in fingerprint.columns
        )
        t = schema_version
        for _ in range(_MAX_RETRIES):
            try:
                with self._engine.begin() as conn:
                    row = conn.execute(
                        select(t).where(
                            t.c.source_name == source_name,
                            t.c.fingerprint == fingerprint.fingerprint,
                        )
                    ).first()
                    if row is not None:
                        return _to_version(row)
                    last = conn.execute(
                        select(t)
                        .where(t.c.source_name == source_name)
                        .order_by(t.c.version.desc())
                        .limit(1)
                    ).first()
                    if last is None:
                        version, semver = 1, "1.0.0"
                    else:
                        kind = classify_bump(_load_columns(last.columns_json), cols)
                        version, semver = last.version + 1, _bump(last.semver, kind)
                    new = SchemaVersion(
                        source_name=source_name,
                        version=version,
                        semver=semver,
                        fingerprint=fingerprint.fingerprint,
                        columns=cols,
                        created_at=datetime.now(UTC),
                    )
                    conn.execute(
                        insert(t).values(
                            source_name=source_name,
                            version=version,
                            semver=semver,
                            fingerprint=new.fingerprint,
                            columns_json=json.dumps([c.model_dump() for c in cols]),
                            created_at=new.created_at,
                        )
                    )
            except IntegrityError:
                continue  # a concurrent writer won the race: re-read
            except SQLAlchemyError as e:
                raise SchemaError(f"schema registry write failed ({type(e).__name__})") from e
            log.info("schema_registered", source_name=source_name, version=version, semver=semver)
            return new
        raise SchemaError(f"{source_name}: register gave up after {_MAX_RETRIES} conflicts")

    def get(self, source_name: str, version: int) -> SchemaVersion | None:
        """Return a given version, or None if unknown."""
        t = schema_version
        rows = self._read(
            select(t).where(t.c.source_name == _check_source(source_name), t.c.version == version)
        )
        return _to_version(rows[0]) if rows else None

    def latest(self, source_name: str) -> SchemaVersion | None:
        """Return the highest version, or None if the source has none."""
        hist = self.history(source_name)
        return hist[-1] if hist else None

    def history(self, source_name: str) -> list[SchemaVersion]:
        """All versions of the source by increasing version (= registration order)."""
        t = schema_version
        stmt = select(t).where(t.c.source_name == _check_source(source_name))
        return [_to_version(r) for r in self._read(stmt.order_by(t.c.version))]

    def record_changes(
        self,
        source_name: str,
        from_version: int | None,
        to_version: int,
        changes: Sequence[SchemaChange],
    ) -> int:
        """Store the changes of a (from, to) pair, replacing any previous ones; return count."""
        _check_source(source_name)
        sv, sc = schema_version, schema_change
        wanted = {v for v in (from_version, to_version) if v is not None}
        same_pair = (
            sc.c.from_version.is_(None)
            if from_version is None
            else sc.c.from_version == from_version
        )
        now = datetime.now(UTC)
        try:
            with self._engine.begin() as conn:
                known = set(
                    conn.execute(
                        select(sv.c.version).where(
                            sv.c.source_name == source_name, sv.c.version.in_(wanted)
                        )
                    ).scalars()
                )
                if missing := wanted - known:
                    raise SchemaError(f"{source_name}: unknown version(s) {sorted(missing)}")
                conn.execute(
                    delete(sc).where(
                        sc.c.source_name == source_name, sc.c.to_version == to_version, same_pair
                    )
                )
                if changes:
                    conn.execute(
                        insert(sc),
                        [
                            {
                                "source_name": source_name,
                                "from_version": from_version,
                                "to_version": to_version,
                                "change_type": ch.change_type,
                                "column_name": ch.column_name,
                                "detail_json": json.dumps(ch.detail, sort_keys=True),
                                "created_at": now,
                            }
                            for ch in changes
                        ],
                    )
        except SQLAlchemyError as e:
            raise SchemaError(f"schema change write failed ({type(e).__name__})") from e
        return len(changes)

    def changes(self, source_name: str, to_version: int) -> list[ChangeRecord]:
        """Changes leading to `to_version`, in insertion order."""
        sc = schema_change
        stmt = select(sc).where(
            sc.c.source_name == _check_source(source_name), sc.c.to_version == to_version
        )
        return [
            ChangeRecord(
                source_name=r.source_name,
                from_version=r.from_version,
                to_version=r.to_version,
                change=SchemaChange(
                    change_type=r.change_type,
                    column_name=r.column_name,
                    detail=json.loads(r.detail_json),
                ),
                created_at=_utc(r.created_at),
            )
            for r in self._read(stmt.order_by(sc.c.id))
        ]

    def _read(self, stmt: Any) -> list[Row[Any]]:
        try:
            with self._engine.connect() as conn:
                return list(conn.execute(stmt))
        except SQLAlchemyError as e:
            raise SchemaError(f"schema registry read failed ({type(e).__name__})") from e
