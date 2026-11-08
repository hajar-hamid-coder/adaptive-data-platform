# Graph Report - adaptive-data-platform  (2026-11-08)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 606 nodes · 1389 edges · 25 communities (21 shown, 4 thin omitted)
- Extraction: 89% EXTRACTED · 11% INFERRED · 0% AMBIGUOUS · INFERRED: 146 edges (avg confidence: 0.91)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `11c25f19`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- Community 0
- Community 1
- Community 2
- Community 3
- Community 4
- Community 5
- Community 6
- Community 7
- Community 8
- Community 9
- Community 10
- Community 11
- Community 12
- Community 13
- Community 14
- Community 15
- Community 16
- Community 17
- Community 18
- Community 19
- Community 20
- Community 22
- Community 23

## God Nodes (most connected - your core abstractions)
1. `IngestionError` - 52 edges
2. `LocalStore` - 35 edges
3. `fetch_all()` - 24 edges
4. `write_bronze()` - 22 edges
5. `ObjectStore` - 21 edges
6. `plan_archive()` - 20 edges
7. `RawTable` - 19 edges
8. `S3Store` - 17 edges
9. `ingest_with_deadletter()` - 17 edges
10. `extract_incremental()` - 17 edges

## Surprising Connections (you probably didn't know these)
- `test_error_hierarchy()` --uses--> `ConfigError`  [INFERRED]
  tests/test_logging.py → src/adp/errors.py
- `store()` --uses--> `LocalStore`  [INFERRED]
  tests/bronze/test_lifecycle.py → src/adp/bronze/store.py
- `test_rejects_travel_with_batch()` --uses--> `LocalStore`  [INFERRED]
  tests/bronze/test_lifecycle.py → src/adp/bronze/store.py
- `test_store_list_ignores_temp_and_prefix()` --uses--> `LocalStore`  [INFERRED]
  tests/bronze/test_lifecycle.py → src/adp/bronze/store.py
- `_run()` --uses--> `LocalStore`  [INFERRED]
  tests/orchestration/test_ingestion.py → src/adp/bronze/store.py

## Import Cycles
- None detected.

## Communities (25 total, 4 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.07
Nodes (31): apply_archive(), ArchiveResult, _Default, load_retention(), Move, _parse(), plan_archive(), RetentionPolicy (+23 more)

### Community 1 - "Community 1"
Cohesion: 0.06
Nodes (20): main(), scan(), list_buckets(), main(), postgres_ready(), _sign(), _sign(), _adrs() (+12 more)

### Community 2 - "Community 2"
Cohesion: 0.07
Nodes (25): max_concurrent_runs, run_coordinator, class, config, module, bind_run_id(), build_definitions(), defs() (+17 more)

### Community 3 - "Community 3"
Cohesion: 0.09
Nodes (13): ingest_with_deadletter(), IngestSummary, Reject, RetryPolicy, _to_reject(), write_rejects(), ObjectStore, batch_hash() (+5 more)

### Community 4 - "Community 4"
Cohesion: 0.09
Nodes (16): _check_ident(), _dump(), extract_incremental(), _parser(), _to_str(), WatermarkStore, engine(), _insert() (+8 more)

### Community 5 - "Community 5"
Cohesion: 0.14
Nodes (20): _cell(), _dig(), fetch_all(), get(), Pagination, _retry_after(), _Retryable, _safe_url() (+12 more)

### Community 6 - "Community 6"
Cohesion: 0.07
Nodes (34): minio, MINIO_ROOT_PASSWORD, MINIO_ROOT_USER, POSTGRES_DB, POSTGRES_PASSWORD, POSTGRES_USER, interval, retries (+26 more)

### Community 7 - "Community 7"
Cohesion: 0.10
Nodes (17): Backlog, cmd_export_md(), cmd_hook_session(), cmd_hook_stop(), cmd_hypo(), cmd_hypo_add(), cmd_set(), cmd_status() (+9 more)

### Community 8 - "Community 8"
Cohesion: 0.15
Nodes (19): BadRow, _csv_with_rejects(), _decode(), _json_with_rejects(), list_sheets(), _open_xlsx(), RawTable, _read_bytes() (+11 more)

### Community 9 - "Community 9"
Cohesion: 0.13
Nodes (15): AdpError, QualityError, SchemaError, SourceSpecError, load_registry(), load_source(), SourceSpec, test_auth_ref_optional() (+7 more)

### Community 10 - "Community 10"
Cohesion: 0.16
Nodes (24): LocalStore, _count(), _csv(), _go(), test_corrupt_data_not_retried(), test_invariant_ok_plus_rejected_equals_read(), test_jsonl_invalid_line(), test_logs_have_no_row_values() (+16 more)

### Community 11 - "Community 11"
Cohesion: 0.12
Nodes (9): get_settings(), Settings, _clean_env(), _load_checker(), test_check_secrets_flags_and_passes(), test_defaults(), test_env_override(), test_get_settings_cached() (+1 more)

### Community 12 - "Community 12"
Cohesion: 0.10
Nodes (21): enabledPlugins, ponytail@ponytail, ui-ux-pro-max@ui-ux-pro-max-skill, extraKnownMarketplaces, ponytail, ui-ux-pro-max-skill, hooks, PostToolUse (+13 more)

### Community 13 - "Community 13"
Cohesion: 0.21
Nodes (7): configure_logging(), get_logger(), _clean(), _lines(), test_json_lines_have_run_id_level_event(), test_level_filtering(), test_run_id_explicit_and_persists()

### Community 14 - "Community 14"
Cohesion: 0.11
Nodes (17): runs-on, steps, strategy, jobs, ci, python-version, name, on (+9 more)

### Community 15 - "Community 15"
Cohesion: 0.32
Nodes (4): S3Store, IngestionError, test_s3_list_and_move_on_minio(), test_minio_roundtrip()

### Community 16 - "Community 16"
Cohesion: 0.18
Nodes (10): entry, files, id, language, name, pass_filenames, types, repos (+2 more)

### Community 17 - "Community 17"
Cohesion: 0.29
Nodes (6): default, archive_prefix, retention_days, sources, world_bank_gdp, retention_days

### Community 18 - "Community 18"
Cohesion: 0.29
Nodes (6): format, frequency, name, primary_key, type, uri

### Community 19 - "Community 19"
Cohesion: 0.29
Nodes (6): format, frequency, name, primary_key, type, uri

### Community 20 - "Community 20"
Cohesion: 0.29
Nodes (6): format, frequency, name, primary_key, type, uri

## Knowledge Gaps
- **1 isolated node(s):** `adp`
  These have ≤1 connection - possible missing edges. (Counts symbols only; 219 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **4 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `IngestionError` connect `Community 15` to `Community 0`, `Community 1`, `Community 3`, `Community 4`, `Community 5`, `Community 8`, `Community 9`, `Community 10`?**
  _High betweenness centrality (0.113) - this node is a cross-community bridge._
- **Are the 16 inferred relationships involving `IngestionError` (e.g. with `RetryPolicy` and `LocalStore`) actually correct?**
  _`IngestionError` has 16 INFERRED edges - model-reasoned connections that need verification._
- **What connects `adp` to the rest of the system?**
  _1 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Community 0` be split into smaller, more focused modules?**
  _Cohesion score 0.07080200501253132 - nodes in this community are weakly interconnected._
- **Why does `LocalStore` connect `Community 10` to `Community 0`, `Community 1`, `Community 2`, `Community 15`?**
  _High betweenness centrality (0.046) - this node is a cross-community bridge._
- **Are the 23 inferred relationships involving `LocalStore` (e.g. with `IngestionError` and `test_corrupt_data_not_retried()`) actually correct?**
  _`LocalStore` has 23 INFERRED edges - model-reasoned connections that need verification._
- **Should `Community 1` be split into smaller, more focused modules?**
  _Cohesion score 0.06363636363636363 - nodes in this community are weakly interconnected._