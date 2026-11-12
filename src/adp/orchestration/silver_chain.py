"""Silver chain in Dagster (H-24): `silver_<source>` = mapping, decision, clean, dedup, write.

Runs downstream of `schema_check_<source>`; Dagster never starts it when the gate fails, so a
blocked schema never reaches Silver. Thin wrapper: all logic lives in `adp.mapping`/`adp.silver`.

Decisions (guard rails of the wiring):
- Batch and version: the asset reads `parquet_path` AND `version` from the latest
  materialization of `schema_check_<source>` (the gate publishes the path of the batch it
  checked), never from `bronze_<source>` directly: Silver always processes the batch the gate
  approved, under the registry version the gate produced for it. A missing version or path is an
  explicit Failure: nothing is guessed (no default version).
- Mapping (`resolve_mapping`): column NAMES of the Bronze batch -> curated dictionary + learned
  synonyms (reloaded on every run, so an approval learned in between applies to the next run;
  a corrupt learned file only drops the learned entries) -> rules -> the LLM mapper for the
  columns the rules leave unresolved (Claude only when a key is configured, else the local
  fallback whose proposals are always review-only). Profiles reach the rules (type
  compatibility) and the mapper WITHOUT `examples`: no data value can reach an LLM. Every
  proposal goes through `ReviewQueue.submit` (H-02 policy + guards, journal): only `auto_apply`
  is applied; `review` stays pending in the queue and the column is NOT mapped until a human
  approves it; `reject`/abstention leaves it unmapped.
- `mapping_incomplete`: a primary-key column (source names of `SourceSpec.primary_key`) that is
  not auto-mapped stops the asset (Failure, allow_retries=False, status `blocked`) BEFORE any
  cleaning or write: Silver without its key cannot be deduplicated nor joined. It is a wait for
  human review, not a technical error. Non-key unmapped columns are dropped and listed.
- Dedup keys = canonical fields of the primary key. A source without primary key is not
  deduplicated (dedupe needs a key; `rows_deduped` = 0).
- Cleaning rejects go to the dead-letter of the SILVER store (`<source>/ingestion_date=D/
  <hash>.rejects.jsonl`, apart from `<source>/schema_v<N>/`): Bronze stays immutable raw data
  and only receives ingestion rejects. Reject rate >= 20 % (H-02) -> Failure `reject_threshold`,
  nothing written to Silver.
- Idempotence: the writer skips an already written logical batch (`skipped=True`); the review
  queue is idempotent per (source, column, target, prompt version).
- Known limit: within one schema_v<N>, approving a pending NON-key column later adds that column
  to the next batches (additive only; read with DuckDB `union_by_name`).
- Metadata, logs and Failure messages carry column names, counts, hashes and paths only.
"""

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

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
from adp.config import get_settings
from adp.connectors.files import RawTable
from adp.errors import TransformError
from adp.logging import bind_run_id, get_logger
from adp.mapping.decision import Proposal, ReviewQueue, combine
from adp.mapping.dictionary import SemanticDictionary
from adp.mapping.learn import DEFAULT_LEARNED_PATH, load_dictionary_with_learned
from adp.mapping.llm import LLMMapper, build_mapper
from adp.mapping.rules import map_columns
from adp.mapping.transform import load_field_transforms
from adp.orchestration.schema_gate import read_bronze_table
from adp.schema.profile import ColumnProfile, profile_table
from adp.silver.clean import clean_table, load_cleaning
from adp.silver.dedup import dedupe
from adp.silver.writer import SilverManifest, write_silver
from adp.sources.spec import SourceSpec

log = get_logger(__name__)
SEMANTIC = Path("config/semantic")
MapperFactory = Callable[[SemanticDictionary], LLMMapper]


def build_silver_store() -> ObjectStore:
    """ADP_STORE=local (ADP_SILVER_STORE_ROOT, default data/silver) or s3 (ADP_S3_SILVER_BUCKET)."""
    if os.environ.get("ADP_STORE", "local") == "s3":
        return S3Store(os.environ.get("ADP_S3_SILVER_BUCKET", "silver"))
    return LocalStore(Path(os.environ.get("ADP_SILVER_STORE_ROOT", "data/silver")))


def load_run_dictionary() -> SemanticDictionary:
    """Curated + learned dictionary, read for each run (ADP_CANONICAL_PATH, ADP_LEARNED_PATH)."""
    canonical = Path(os.environ.get("ADP_CANONICAL_PATH", str(SEMANTIC / "canonical.yaml")))
    learned = Path(os.environ.get("ADP_LEARNED_PATH", str(DEFAULT_LEARNED_PATH)))
    return load_dictionary_with_learned(canonical, learned)


@dataclass(frozen=True)
class MappingOutcome:
    """Applied mapping {source column: canonical field} and what is left aside (names only)."""

    mapping: dict[str, str]
    mapped: list[dict[str, Any]]  # [{source, target, method, score}] of auto-applied columns
    pending: list[str]  # columns waiting for human review
    unmapped: list[str]  # every column not applied (pending, rejected, no proposal)


def resolve_mapping(
    columns: Sequence[str],
    profiles: Mapping[str, ColumnProfile],
    *,
    dictionary: SemanticDictionary,
    mapper: LLMMapper,
    queue: ReviewQueue,
    source: str,
) -> MappingOutcome:
    """Rules, then the mapper on unresolved columns; only `auto_apply` decisions are applied."""
    bare = {c: p.model_copy(update={"examples": ()}) for c, p in profiles.items()}
    rules = map_columns(columns, dictionary, mapper.rules_config, bare)
    llm = mapper.map(list(columns), bare)  # {} when the rules resolved every column
    proposals: list[Proposal] = []
    for c in columns:
        merged = combine(rules[c][0] if rules[c] else None, llm.get(c))
        if merged is not None and merged.source_col:
            proposals.append(merged)
    result = queue.submit(proposals, source)
    mapping = {d.proposal.source_col: d.proposal.target or "" for d in result.auto_applied}
    mapped = [
        {"source": d.proposal.source_col, "target": d.proposal.target,
         "method": d.proposal.method, "score": d.proposal.score}
        for d in result.auto_applied
    ]  # fmt: skip
    vetoed = {d.proposal.source_col for d in result.vetoed}
    pending = [
        d.proposal.source_col
        for d in result.decisions
        if d.action == "review" and d.proposal.source_col not in vetoed
    ]
    return MappingOutcome(mapping, mapped, pending, [c for c in columns if c not in mapping])


@dataclass(frozen=True)
class SilverChainResult:
    """Outcome of one Silver run (no data value)."""

    status: Literal["written", "blocked"]
    outcome: MappingOutcome
    cause: str | None = None
    missing_key_columns: list[str] = field(default_factory=list)
    rows_in: int = 0
    rows_cleaned: int = 0
    rows_rejected: int = 0
    rows_deduped: int = 0
    corrections: dict[str, int] = field(default_factory=dict)
    rejects_path: str | None = None
    manifest: SilverManifest | None = None


def run_silver_chain(
    table: RawTable,
    *,
    spec: SourceSpec,
    schema_version: int,
    dictionary: SemanticDictionary,
    mapper: LLMMapper,
    queue: ReviewQueue,
    store: ObjectStore,
    run_id: str,
    semantic_dir: Path = SEMANTIC,
) -> SilverChainResult:
    """Map, clean, deduplicate and write one approved Bronze batch (pure of Dagster).

    Raises TransformError when the cleaning reject rate reaches the H-02 threshold.
    """
    profiles = {p.name: p for p in profile_table(table).columns}
    out = resolve_mapping(
        table.columns, profiles, dictionary=dictionary, mapper=mapper, queue=queue,
        source=spec.name,
    )  # fmt: skip
    missing = [c for c in spec.primary_key if c not in out.mapping]
    if missing:
        log.warning("silver_mapping_incomplete", source=spec.name, run_id=run_id,
                    missing=missing, pending=out.pending)  # fmt: skip
        return SilverChainResult(
            status="blocked", outcome=out, cause="mapping_incomplete",
            missing_key_columns=missing, rows_in=len(table.rows),
        )  # fmt: skip
    transforms = load_field_transforms(semantic_dir / "transforms.yaml", dictionary=dictionary)
    text_rules = load_cleaning(semantic_dir / "cleaning.yaml", dictionary)
    cleaned = clean_table(
        table, out.mapping, dictionary=dictionary, transforms=transforms,
        text_rules=text_rules, source=spec.name, run_id=run_id, store=store,
    )  # fmt: skip
    keys = [out.mapping[c] for c in spec.primary_key]
    rows = cleaned.rows
    if keys:
        rows = dedupe(cleaned.columns, cleaned.rows, key_fields=keys).survivors
    deduped = len(cleaned.rows) - len(rows)
    manifest = write_silver(
        (cleaned.columns, rows), source=spec.name, schema_version=schema_version,
        run_id=run_id, store=store, dictionary=dictionary, rows_in=len(table.rows),
        rows_deduped=deduped, rows_rejected=len(cleaned.rejects),
    )  # fmt: skip
    return SilverChainResult(
        status="written", outcome=out, rows_in=len(table.rows), rows_cleaned=len(cleaned.rows),
        rows_rejected=len(cleaned.rejects), rows_deduped=deduped,
        corrections={f"{t}:{k}": n for (t, k), n in sorted(cleaned.corrections.counts.items())},
        rejects_path=cleaned.rejects_path, manifest=manifest,
    )  # fmt: skip


def _default_mapper(dictionary: SemanticDictionary) -> LLMMapper:
    return build_mapper(get_settings(), dictionary)


def _metadata(res: SilverChainResult, schema_version: int) -> dict[str, Any]:
    mf, out = res.manifest, res.outcome
    return {
        "status": res.status,
        "cause": res.cause or "",
        "schema_version": schema_version,
        "rows_in": res.rows_in,
        "rows_cleaned": res.rows_cleaned,
        "rows_rejected": res.rows_rejected,
        "rows_deduped": res.rows_deduped,
        "rows_written": mf.row_count if mf else 0,
        "batch_hash": MetadataValue.text(mf.batch_hash if mf else ""),
        "silver_path": MetadataValue.text(mf.parquet_path if mf else ""),
        "skipped": bool(mf and mf.skipped),
        "rejects_path": MetadataValue.text(res.rejects_path or ""),
        "mapped": MetadataValue.json(out.mapped),
        "pending_review": len(out.pending),
        "pending_columns": MetadataValue.json(out.pending),
        "missing_key_columns": MetadataValue.json(res.missing_key_columns),
        "unmapped": MetadataValue.json(out.unmapped),
        "corrections": MetadataValue.json(res.corrections),
    }


def make_silver_asset(
    spec: SourceSpec, mapper_factory: MapperFactory | None = None
) -> AssetsDefinition:
    """Build `silver_<source>`, downstream of `schema_check_<source>`."""
    check = AssetKey(f"schema_check_{spec.name}")

    @asset(
        name=f"silver_{spec.name}", group_name="silver", deps=[check],
        required_resource_keys={"store", "silver_store", "schema_registry"},
    )  # fmt: skip
    def _silver(context: OpExecutionContext) -> MaterializeResult[None]:
        run_id = bind_run_id(context.run_id)
        event = context.instance.get_latest_materialization_event(check)
        meta = event.asset_materialization.metadata if event and event.asset_materialization else {}
        version = meta["version"].value if "version" in meta else None
        path = str(meta["parquet_path"].value) if "parquet_path" in meta else ""
        if type(version) is not int or version < 1 or not path:
            raise Failure(
                description=f"{spec.name}: schema gate gave no registry version or batch path",
                allow_retries=False,
            )
        dictionary = load_run_dictionary()
        factory = mapper_factory or _default_mapper
        try:
            res = run_silver_chain(
                read_bronze_table(context.resources.store, path), spec=spec,
                schema_version=version, dictionary=dictionary, mapper=factory(dictionary),
                queue=ReviewQueue(context.resources.schema_registry.engine(), dictionary),
                store=context.resources.silver_store, run_id=run_id,
            )  # fmt: skip
        except TransformError as exc:
            raise Failure(
                description=f"{spec.name}: Silver stopped (reject_threshold): {exc}",
                metadata={"status": "blocked", "cause": "reject_threshold"},
                allow_retries=False,
            ) from exc
        md = _metadata(res, version)
        if res.status == "blocked":
            raise Failure(
                description=(
                    f"Silver blocked (mapping_incomplete) for {spec.name}: key columns "
                    f"{', '.join(res.missing_key_columns)} not mapped automatically; "
                    f"{len(res.outcome.pending)} column(s) waiting for human review"
                ),
                metadata=md, allow_retries=False,
            )  # fmt: skip
        return MaterializeResult(metadata=md)

    return _silver


def make_silver_assets(
    registry: dict[str, SourceSpec], mapper_factory: MapperFactory | None = None
) -> list[AssetsDefinition]:
    """One Silver asset per `file` source (same set as the Bronze and schema check assets)."""
    return [make_silver_asset(s, mapper_factory) for s in registry.values() if s.type == "file"]
