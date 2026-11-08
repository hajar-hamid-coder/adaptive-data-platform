# Graph Report - adaptive-data-platform  (2026-11-08)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 1120 nodes · 2941 edges · 58 communities (50 shown, 8 thin omitted)
- Extraction: 84% EXTRACTED · 16% INFERRED · 0% AMBIGUOUS · INFERRED: 465 edges (avg confidence: 0.92)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `12fc398f`
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
- Community 21
- Community 22
- Community 23
- Community 24
- Community 25
- Community 26
- Community 27
- Community 28
- Community 29
- Community 30
- Community 31
- Community 32
- Community 33
- Community 34
- Community 35
- Community 36
- Community 37
- Community 38
- Community 39
- Community 40
- Community 41
- Community 42
- Community 43
- Community 44
- Community 45
- Community 46
- Community 47
- Community 48
- Community 49
- Community 50
- Community 51
- Community 53
- Community 54
- Community 56

## God Nodes (most connected - your core abstractions)
1. `SchemaRegistry` - 58 edges
2. `IngestionError` - 52 edges
3. `diff_schemas()` - 47 edges
4. `ChangeKind` - 41 edges
5. `EvolutionPolicy` - 40 edges
6. `LocalStore` - 38 edges
7. `RawTable` - 32 edges
8. `profile_table()` - 28 edges
9. `SchemaFingerprint` - 27 edges
10. `ChangeImpact` - 27 edges

## Surprising Connections (you probably didn't know these)
- `test_error_hierarchy()` --uses--> `SchemaError`  [INFERRED]
  tests/test_logging.py → src/adp/errors.py
- `_pure_gate()` --uses--> `SchemaRegistry`  [INFERRED]
  tests/orchestration/test_schema_gate.py → src/adp/schema/registry.py
- `test_pure_gate_event_once_and_no_values()` --uses--> `SchemaRegistry`  [INFERRED]
  tests/orchestration/test_schema_gate.py → src/adp/schema/registry.py
- `reg()` --uses--> `SchemaRegistry`  [INFERRED]
  tests/schema/test_report.py → src/adp/schema/registry.py
- `run()` --uses--> `SchemaRegistry`  [INFERRED]
  tests/schema/test_report.py → src/adp/schema/registry.py

## Import Cycles
- None detected.

## Communities (58 total, 8 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.05
Nodes (47): SchemaError, _found_in_log(), _bump(), ChangeRecord, _check_source(), classify_bump(), _load_columns(), SchemaChange (+39 more)

### Community 1 - "Community 1"
Cohesion: 0.07
Nodes (37): BadRow, _csv_with_rejects(), _decode(), _json_with_rejects(), list_sheets(), _open_xlsx(), RawTable, _read_bytes() (+29 more)

### Community 2 - "Community 2"
Cohesion: 0.05
Nodes (42): id, impact, reason, when, default, impact, reason, id (+34 more)

### Community 3 - "Community 3"
Cohesion: 0.14
Nodes (20): _cell(), _dig(), fetch_all(), get(), Pagination, _retry_after(), _Retryable, _safe_url() (+12 more)

### Community 4 - "Community 4"
Cohesion: 0.09
Nodes (16): main(), scan(), _parse_number(), _adrs(), test_adr_has_required_sections(), test_architecture_has_mermaid_flowchart(), test_architecture_links_to_existing_adrs(), test_at_least_six_numbered_adrs() (+8 more)

### Community 5 - "Community 5"
Cohesion: 0.10
Nodes (27): _date_hint_from(), _is_datetime(), profile_column(), _valid_date(), hint(), t(), table(), test_ambiguous_thousands() (+19 more)

### Community 6 - "Community 6"
Cohesion: 0.07
Nodes (34): minio, MINIO_ROOT_PASSWORD, MINIO_ROOT_USER, POSTGRES_DB, POSTGRES_PASSWORD, POSTGRES_USER, interval, retries (+26 more)

### Community 7 - "Community 7"
Cohesion: 0.10
Nodes (17): Backlog, cmd_export_md(), cmd_hook_session(), cmd_hook_stop(), cmd_hypo(), cmd_hypo_add(), cmd_set(), cmd_status() (+9 more)

### Community 8 - "Community 8"
Cohesion: 0.13
Nodes (14): ColumnProfile, names(), non_reorder(), of(), test_added_removed_symmetry_after_matching(), test_added_removed_symmetry_without_renames(), test_attribute_changes_of_same_name_columns(), test_candidate_scores_are_symmetric() (+6 more)

### Community 9 - "Community 9"
Cohesion: 0.16
Nodes (17): EvolutionPolicy, render_json(), drift(), engine(), make_bus(), policy(), reg(), run() (+9 more)

### Community 10 - "Community 10"
Cohesion: 0.12
Nodes (12): SchemaChanged, EvolutionAction, build_report(), _cell(), _column_text(), EvolutionReport, _kind_text(), RenameScore (+4 more)

### Community 11 - "Community 11"
Cohesion: 0.14
Nodes (15): dates(), floats(), gen(), ints(), pick(), _same_values(), seq(), table() (+7 more)

### Community 12 - "Community 12"
Cohesion: 0.15
Nodes (7): batch_hash(), BronzeManifest, row_hash(), _skipped(), _to_parquet(), write_bronze(), _digest()

### Community 13 - "Community 13"
Cohesion: 0.16
Nodes (10): ObjectStore, GateResult, _known(), _load_profile(), _profile_key(), read_bronze_table(), _report_key(), run_schema_gate() (+2 more)

### Community 14 - "Community 14"
Cohesion: 0.13
Nodes (7): _check_ident(), _dump(), extract_incremental(), _parser(), _to_str(), WatermarkStore, test_unreachable_database_error_hides_secret()

### Community 15 - "Community 15"
Cohesion: 0.14
Nodes (12): _close(), _columns(), DiffConfig, dist_similarity(), _iou(), _is_abbrev(), name_similarity(), _score() (+4 more)

### Community 16 - "Community 16"
Cohesion: 0.19
Nodes (10): SourceSpecError, load_registry(), load_source(), test_auth_ref_optional(), test_invalid_yaml_raises_clear_error(), test_missing_file_raises(), test_registry_loads_and_rejects_duplicates(), test_shipped_example_sources_are_valid() (+2 more)

### Community 17 - "Community 17"
Cohesion: 0.10
Nodes (21): enabledPlugins, ponytail@ponytail, ui-ux-pro-max@ui-ux-pro-max-skill, extraKnownMarketplaces, ponytail, ui-ux-pro-max-skill, hooks, PostToolUse (+13 more)

### Community 18 - "Community 18"
Cohesion: 0.12
Nodes (9): ClassifiedChange, Conditions, DefaultRule, DiffImpact, _facts(), Rule, type_shift(), with_impact() (+1 more)

### Community 19 - "Community 19"
Cohesion: 0.23
Nodes (13): load_policy(), _permissive_policy(), policy(), test_empty_rule_list_rejected(), test_invalid_policy_rejected(), test_missing_file(), test_modified_policy_changes_result(), test_non_mapping_rejected() (+5 more)

### Community 20 - "Community 20"
Cohesion: 0.14
Nodes (9): apply_archive(), ArchiveResult, _Default, load_retention(), Move, _SourceRule, summarize(), _archive() (+1 more)

### Community 21 - "Community 21"
Cohesion: 0.15
Nodes (5): BronzeConfig, build_store(), landing_dir(), make_landing_sensor(), _sensor()

### Community 22 - "Community 22"
Cohesion: 0.24
Nodes (14): ChangeKind, ChangeImpact, classify_change(), ch(), expected_shift(), fp(), test_empty_primary_key_disables_key_rules(), test_int_to_float_widening_is_compatible() (+6 more)

### Community 23 - "Community 23"
Cohesion: 0.18
Nodes (15): diff_schemas(), drift(), expected(), _ops(), test_column_profile_list_input(), test_deterministic_and_input_order_independent(), test_duplicate_column_rejected(), test_empty_schemas() (+7 more)

### Community 24 - "Community 24"
Cohesion: 0.21
Nodes (7): configure_logging(), get_logger(), _clean(), _lines(), test_json_lines_have_run_id_level_event(), test_level_filtering(), test_run_id_explicit_and_persists()

### Community 25 - "Community 25"
Cohesion: 0.22
Nodes (9): engine(), _insert(), _pg_engine(), _store(), table(), test_error_mid_read_keeps_watermark(), test_first_read_all_then_nothing_then_five(), test_invalid_identifiers() (+1 more)

### Community 26 - "Community 26"
Cohesion: 0.11
Nodes (17): runs-on, steps, strategy, jobs, ci, python-version, name, on (+9 more)

### Community 27 - "Community 27"
Cohesion: 0.15
Nodes (7): Settings, _clean_env(), _load_checker(), test_check_secrets_flags_and_passes(), test_defaults(), test_env_override(), test_secrets_masked()

### Community 28 - "Community 28"
Cohesion: 0.18
Nodes (5): list_buckets(), main(), postgres_ready(), _sign(), _sign()

### Community 29 - "Community 29"
Cohesion: 0.13
Nodes (3): get_settings(), pg_engine(), test_get_settings_cached()

### Community 30 - "Community 30"
Cohesion: 0.34
Nodes (4): S3Store, IngestionError, test_s3_list_and_move_on_minio(), test_minio_roundtrip()

### Community 31 - "Community 31"
Cohesion: 0.18
Nodes (6): ingest_with_deadletter(), IngestSummary, Reject, RetryPolicy, _to_reject(), write_rejects()

### Community 32 - "Community 32"
Cohesion: 0.31
Nodes (13): _count(), _csv(), _go(), test_corrupt_data_not_retried(), test_invariant_ok_plus_rejected_equals_read(), test_jsonl_invalid_line(), test_logs_have_no_row_values(), test_replay_no_duplicates() (+5 more)

### Community 33 - "Community 33"
Cohesion: 0.18
Nodes (6): _parse(), plan_archive(), RetentionPolicy, test_parquet_and_manifest_together_manifest_last(), test_resume_after_crash_before_manifest(), test_source_override()

### Community 34 - "Community 34"
Cohesion: 0.22
Nodes (11): main(), _batch(), _snapshot(), store(), test_cli_bad_config_exit_code(), test_cli_bad_date(), test_cli_summary_by_source(), test_dry_run_changes_nothing_and_matches_real() (+3 more)

### Community 35 - "Community 35"
Cohesion: 0.18
Nodes (4): build_definitions(), defs(), SchemaRegistryResource, test_definitions_load()

### Community 37 - "Community 37"
Cohesion: 0.32
Nodes (10): _files(), test_crash_between_parquet_and_manifest(), put_if_absent(), test_different_batch_new_file(), test_manifest_content(), test_replay_is_noop_and_file_untouched(), test_reserved_column_rejected(), test_row_hash_stable_and_sensitive() (+2 more)

### Community 38 - "Community 38"
Cohesion: 0.27
Nodes (5): LocalStore, test_destination_differs_loses_nothing(), test_rejects_travel_with_batch(), test_source_and_destination_exist(), test_store_list_ignores_temp_and_prefix()

### Community 39 - "Community 39"
Cohesion: 0.23
Nodes (7): bind_run_id(), _bronze(), make_schema_check_asset(), _check(), EventBus, profile_table(), gate()

### Community 40 - "Community 40"
Cohesion: 0.18
Nodes (4): _attribute_changes(), ColumnChange, RenameCandidate, SchemaDiff

### Community 41 - "Community 41"
Cohesion: 0.18
Nodes (10): entry, files, id, language, name, pass_filenames, types, repos (+2 more)

### Community 42 - "Community 42"
Cohesion: 0.27
Nodes (4): make_bronze_asset(), make_bronze_assets(), make_schema_check_assets(), SourceSpec

### Community 43 - "Community 43"
Cohesion: 0.29
Nodes (8): classify_diff(), drift(), test_empty_diff_is_compatible(), test_every_decision_is_traceable_and_deterministic(), test_reason_holds_no_data_value(), test_simulator_v1_to_v2_compatible(), test_simulator_v2_to_v3_review(), test_with_impact_annotates_schema_changes()

### Community 44 - "Community 44"
Cohesion: 0.51
Nodes (8): _meta(), _run(), _spec(), test_corrupt_file_goes_to_deadletter(), test_materialize_and_replay(), test_non_file_sources_have_no_asset(), test_sensor_path_runs_asset(), test_sensor_skip_then_run_then_no_retrigger()

### Community 45 - "Community 45"
Cohesion: 0.32
Nodes (4): AdpError, ConfigError, QualityError, test_error_hierarchy()

### Community 46 - "Community 46"
Cohesion: 0.29
Nodes (6): default, archive_prefix, retention_days, sources, world_bank_gdp, retention_days

### Community 47 - "Community 47"
Cohesion: 0.29
Nodes (6): format, frequency, name, primary_key, type, uri

### Community 48 - "Community 48"
Cohesion: 0.29
Nodes (6): format, frequency, name, primary_key, type, uri

### Community 49 - "Community 49"
Cohesion: 0.29
Nodes (6): format, frequency, name, primary_key, type, uri

### Community 50 - "Community 50"
Cohesion: 0.40
Nodes (5): max_concurrent_runs, run_coordinator, class, config, module

## Knowledge Gaps
- **1 isolated node(s):** `adp`
  These have ≤1 connection - possible missing edges. (Counts symbols only; 399 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **8 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `SchemaRegistry` connect `Community 0` to `Community 1`, `Community 35`, `Community 9`, `Community 10`, `Community 13`?**
  _High betweenness centrality (0.036) - this node is a cross-community bridge._
- **Are the 43 inferred relationships involving `SchemaRegistry` (e.g. with `_found_in_log()` and `_known()`) actually correct?**
  _`SchemaRegistry` has 43 INFERRED edges - model-reasoned connections that need verification._
- **What connects `adp` to the rest of the system?**
  _1 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Community 0` be split into smaller, more focused modules?**
  _Cohesion score 0.05259835893120135 - nodes in this community are weakly interconnected._
- **Why does `IngestionError` connect `Community 30` to `Community 32`, `Community 33`, `Community 1`, `Community 3`, `Community 37`, `Community 38`, `Community 12`, `Community 45`, `Community 14`, `Community 16`, `Community 20`, `Community 25`, `Community 28`, `Community 31`?**
  _High betweenness centrality (0.035) - this node is a cross-community bridge._
- **Are the 16 inferred relationships involving `IngestionError` (e.g. with `RetryPolicy` and `LocalStore`) actually correct?**
  _`IngestionError` has 16 INFERRED edges - model-reasoned connections that need verification._
- **Should `Community 1` be split into smaller, more focused modules?**
  _Cohesion score 0.07259615384615385 - nodes in this community are weakly interconnected._