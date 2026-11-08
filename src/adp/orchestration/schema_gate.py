"""Schema gate between Bronze and Silver (P2-06): profile -> diff -> policy -> pass or block.

Decisions:
- Only what the policy classes `AUTO_APPLY` (COMPATIBLE) goes on to Silver. `BLOCK` (impact
  BLOCKING, cause `blocking_change`) AND `QUEUE_REVIEW` (impact REVIEW, cause `review_required`)
  stop the batch for that schema: a batch waiting for human review must not feed Silver.
- Bronze is immutable (ADR 0001): the gate never writes to or deletes from Bronze. It only
  stops what follows.
- The new schema is registered (and its report written) even when blocked: the registry is a
  catalog of seen schemas, so the event and the evidence exist. Replaying a blocked batch stays
  blocked (the decision is re-derived from the change log stored in the registry); an earlier,
  compatible schema (e.g. back to v1) passes. Releasing a blocked version after review is the
  job of a later task.
- First version of a source, or a schema already known without incompatible changes: passed.
- State kept next to Bronze in the object store under `_schema/<source>/`: the report
  `v<N>.report.md` and the profile `<fingerprint>.profile.json` of every schema seen (the
  registry stores no statistics, but `report_evolution` needs the PREVIOUS profile for the
  rename score). Both are written once (put_if_absent). Profiles are stored without `examples`,
  so no raw value is persisted; reports, reasons and logs hold names, rule ids and static
  reasons only.
- Profile = the Bronze parquet of THIS batch, minus technical columns, so it reflects the data
  actually ingested.
- Dagster: `schema_check_<source>` returns metadata `status=passed`; when blocked it raises a
  `dagster.Failure` carrying `status=blocked`, `cause`, `reasons`, `report_path` in its
  metadata. The run ends in a controlled FAILURE and downstream assets (Silver) do not run.
"""

import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

import duckdb
from dagster import (
    AssetKey,
    AssetsDefinition,
    ConfigurableResource,
    Failure,
    MaterializeResult,
    MetadataValue,
    OpExecutionContext,
    asset,
)
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine, create_engine

from adp.bronze.store import ObjectStore
from adp.bronze.writer import TECH_COLUMNS
from adp.config import get_settings
from adp.connectors.files import RawTable
from adp.errors import SchemaError
from adp.logging import bind_run_id, get_logger
from adp.schema.events import EventBus
from adp.schema.policy import ChangeImpact, EvolutionAction, EvolutionPolicy, load_policy
from adp.schema.profile import SchemaFingerprint, profile_table
from adp.schema.registry import SchemaRegistry, metadata
from adp.schema.report import render_markdown, report_evolution
from adp.sources.spec import SourceSpec

log = get_logger(__name__)
STATE_PREFIX = "_schema"
DEFAULT_POLICY = "config/evolution_policy.yaml"
DEFAULT_REGISTRY_URL = "sqlite:///data/registry.db"
_SEVERITY = {
    EvolutionAction.AUTO_APPLY: 0,
    EvolutionAction.QUEUE_REVIEW: 1,
    EvolutionAction.BLOCK: 2,
}
_IMPACT = {
    EvolutionAction.AUTO_APPLY: ChangeImpact.COMPATIBLE,
    EvolutionAction.QUEUE_REVIEW: ChangeImpact.REVIEW,
    EvolutionAction.BLOCK: ChangeImpact.BLOCKING,
}


class GateResult(BaseModel):
    """Outcome of the gate for one batch (no data values)."""

    model_config = ConfigDict(frozen=True)

    status: Literal["passed", "blocked"]
    cause: Literal["blocking_change", "review_required"] | None
    version: int
    semver: str
    overall_impact: ChangeImpact
    overall_action: EvolutionAction
    reasons: list[str]
    report_path: str | None


def read_bronze_table(store: ObjectStore, parquet_path: str) -> RawTable:
    """Read a Bronze parquet back as a RawTable, without the technical columns."""
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "b.parquet"
        local.write_bytes(store.get(parquet_path))
        con = duckdb.connect()
        try:
            cur = con.execute("SELECT * FROM read_parquet(?)", [str(local)])
            names = [d[0] for d in cur.description]
            keep = [i for i, n in enumerate(names) if n not in TECH_COLUMNS]
            rows = [[r[i] for i in keep] for r in cur.fetchall()]
        finally:
            con.close()
    return RawTable(columns=[names[i] for i in keep], rows=rows)


def _profile_key(source: str, fingerprint: str) -> str:
    return f"{STATE_PREFIX}/{source}/{fingerprint}.profile.json"


def _report_key(source: str, version: int) -> str:
    return f"{STATE_PREFIX}/{source}/v{version}.report.md"


def _save_profile(store: ObjectStore, source: str, fp: SchemaFingerprint) -> None:
    bare = fp.model_copy(
        update={"columns": [c.model_copy(update={"examples": ()}) for c in fp.columns]}
    )
    store.put_if_absent(_profile_key(source, fp.fingerprint), bare.model_dump_json().encode())


def _load_profile(store: ObjectStore, source: str, fingerprint: str) -> SchemaFingerprint | None:
    key = _profile_key(source, fingerprint)
    return SchemaFingerprint.model_validate_json(store.get(key)) if store.exists(key) else None


def run_schema_gate(
    fingerprint: SchemaFingerprint,
    *,
    source_name: str,
    primary_key: Sequence[str],
    registry: SchemaRegistry,
    policy: EvolutionPolicy,
    bus: EventBus,
    store: ObjectStore,
    run_id: str | None = None,
) -> GateResult:
    """Register/diff/classify the batch schema and decide: passed or blocked (pure of Dagster)."""
    _save_profile(store, source_name, fingerprint)
    latest = registry.latest(source_name)
    previous = _load_profile(store, source_name, latest.fingerprint) if latest else None
    report = report_evolution(
        registry, source_name, fingerprint, policy=policy, primary_key=primary_key,
        bus=bus, previous=previous, run_id=run_id,
    )  # fmt: skip
    # (kind, column, rule_id, action, reason) of every change leading to the matched version
    found: list[tuple[str, str | None, str, EvolutionAction, str | None]]
    if report is not None:
        version, semver = report.to_version, report.to_semver
        found = [(c.kind.value, c.column, c.rule_id, c.action, c.reason) for c in report.changes]
        key = _report_key(source_name, version)
        store.put_if_absent(key, render_markdown(report).encode())
    else:  # known schema: decide again from the stored change log
        known = next(
            v for v in registry.history(source_name) if v.fingerprint == fingerprint.fingerprint
        )
        version, semver = known.version, known.semver
        found = [
            (r.change.change_type, r.change.column_name, str(r.change.detail.get("rule_id")),
             EvolutionAction(r.change.detail["action"]), None)
            for r in registry.changes(source_name, version)
        ]  # fmt: skip
        key = _report_key(source_name, version)
    action = max(
        (f[3] for f in found), key=_SEVERITY.__getitem__, default=EvolutionAction.AUTO_APPLY
    )
    reasons = [
        f"{kind} {col or '-'} [{rule}] {reason or action_.value}"
        for kind, col, rule, action_, reason in found
        if action_ is not EvolutionAction.AUTO_APPLY
    ]
    blocked = action is not EvolutionAction.AUTO_APPLY
    cause: Literal["blocking_change", "review_required"] | None = None
    if blocked:
        cause = "blocking_change" if action is EvolutionAction.BLOCK else "review_required"
    result = GateResult(
        status="blocked" if blocked else "passed", cause=cause, version=version, semver=semver,
        overall_impact=_IMPACT[action], overall_action=action, reasons=reasons,
        report_path=key if store.exists(key) else None,
    )  # fmt: skip
    log.info(
        "schema_gate", source_name=source_name, status=result.status, cause=cause,
        version=version, run_id=run_id,
    )  # fmt: skip
    return result


class SchemaRegistryResource(ConfigurableResource):  # type: ignore[type-arg]
    """Schema registry connection, resolved lazily from env `ADP_REGISTRY_URL`.

    Default: local SQLite file (`data/registry.db`, tables created on the fly: dev/test only).
    `postgres` uses the metadata Postgres from Settings; its tables come from Alembic.
    """

    url: str = ""

    def registry(self) -> SchemaRegistry:
        url = self.url or os.environ.get("ADP_REGISTRY_URL", DEFAULT_REGISTRY_URL)
        engine: Engine
        if url == "postgres":
            engine = create_engine(get_settings().postgres_url())
        else:
            if url.startswith("sqlite:///"):
                Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
            engine = create_engine(url)
            if url.startswith("sqlite"):
                metadata.create_all(engine)
        return SchemaRegistry(engine)


def make_schema_check_asset(spec: SourceSpec) -> AssetsDefinition:
    """Build `schema_check_<source>`, downstream of `bronze_<source>`."""
    bronze = AssetKey(f"bronze_{spec.name}")

    @asset(
        name=f"schema_check_{spec.name}", group_name="schema", deps=[bronze],
        required_resource_keys={"store", "schema_registry"},
    )  # fmt: skip
    def _check(context: OpExecutionContext) -> MaterializeResult[None]:
        run_id = bind_run_id(context.run_id)
        event = context.instance.get_latest_materialization_event(bronze)
        meta = event.asset_materialization.metadata if event and event.asset_materialization else {}
        path = str(meta["parquet_path"].value) if "parquet_path" in meta else ""
        if not path:
            raise Failure(description=f"{spec.name}: no Bronze batch to check", allow_retries=False)
        store: ObjectStore = context.resources.store
        policy = load_policy(Path(os.environ.get("ADP_POLICY_PATH", DEFAULT_POLICY)))
        try:
            res = run_schema_gate(
                profile_table(read_bronze_table(store, path)),
                source_name=spec.name, primary_key=spec.primary_key,
                registry=context.resources.schema_registry.registry(), policy=policy,
                bus=EventBus(), store=store, run_id=run_id,
            )  # fmt: skip
        except SchemaError as exc:
            raise Failure(description=f"{spec.name}: {exc}", allow_retries=False) from exc
        md = {
            "status": res.status,
            "cause": res.cause or "",
            "version": res.version,
            "semver": MetadataValue.text(res.semver),
            "overall_impact": res.overall_impact.value,
            "overall_action": res.overall_action.value,
            "report_path": MetadataValue.text(res.report_path or ""),
            "reasons": MetadataValue.text("\n".join(res.reasons)),
        }
        if res.status == "blocked":
            raise Failure(
                description=(
                    f"Schema blocked ({res.cause}) for {spec.name} v{res.version}: "
                    + "; ".join(res.reasons)
                ),
                metadata=md, allow_retries=False,
            )  # fmt: skip
        return MaterializeResult(metadata=md)

    return _check


def make_schema_check_assets(registry: dict[str, SourceSpec]) -> list[AssetsDefinition]:
    """One schema check per `file` source (same set as the Bronze assets)."""
    return [make_schema_check_asset(s) for s in registry.values() if s.type == "file"]
