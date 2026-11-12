# Graph Report - adaptive-data-platform  (2026-11-12)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 1998 nodes · 5635 edges · 96 communities (79 shown, 17 thin omitted)
- Extraction: 85% EXTRACTED · 15% INFERRED · 0% AMBIGUOUS · INFERRED: 818 edges (avg confidence: 0.92)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `c4a12fc5`
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
- Community 52
- Community 53
- Community 54
- Community 55
- Community 56
- Community 57
- Community 58
- Community 59
- Community 60
- Community 61
- Community 62
- Community 63
- Community 64
- Community 65
- Community 66
- Community 67
- Community 68
- Community 69
- Community 70
- Community 71
- Community 72
- Community 73
- Community 74
- Community 75
- Community 76
- Community 77
- Community 78
- Community 79
- Community 80
- Community 81
- Community 82
- Community 83
- Community 84
- Community 85
- Community 87
- Community 88
- Community 89
- Community 90
- Community 94

## God Nodes (most connected - your core abstractions)
1. `SemanticDictionary` - 65 edges
2. `SchemaRegistry` - 59 edges
3. `IngestionError` - 53 edges
4. `ReviewQueue` - 47 edges
5. `diff_schemas()` - 47 edges
6. `LocalStore` - 45 edges
7. `RawTable` - 42 edges
8. `ChangeKind` - 41 edges
9. `EvolutionPolicy` - 40 edges
10. `ColumnProfile` - 39 edges

## Surprising Connections (you probably didn't know these)
- `test_cli_bad_config_exit_code()` --calls--> `main()`  [INFERRED]
  tests/bronze/test_lifecycle.py → src/adp/cli.py
- `test_cli_bad_date()` --calls--> `main()`  [INFERRED]
  tests/bronze/test_lifecycle.py → src/adp/cli.py
- `test_cli_summary_by_source()` --calls--> `main()`  [INFERRED]
  tests/bronze/test_lifecycle.py → src/adp/cli.py
- `test_error_hierarchy()` --uses--> `MappingError`  [INFERRED]
  tests/mapping/test_transform.py → src/adp/errors.py
- `entry()` --uses--> `LogEntry`  [INFERRED]
  tests/mapping/test_learn.py → src/adp/mapping/decision.py

## Import Cycles
- None detected.

## Communities (96 total, 17 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.05
Nodes (65): MappingError, _actor(), combine(), decide(), decide_batch(), Decision, DecisionConfig, _entry() (+57 more)

### Community 1 - "Community 1"
Cohesion: 0.05
Nodes (48): _at(), _cascade(), check_criterion(), _ci(), confusion(), _confusion_table(), errors(), _errors_table() (+40 more)

### Community 2 - "Community 2"
Cohesion: 0.07
Nodes (39): S3Store, _csv_with_rejects(), _decode(), _json_with_rejects(), list_sheets(), _open_xlsx(), _read_bytes(), _read_csv() (+31 more)

### Community 3 - "Community 3"
Cohesion: 0.07
Nodes (38): analyze(), refuse(), compute_learned(), _is_human(), LearnedSynonym, LearnResult, _parse(), _read_raw() (+30 more)

### Community 4 - "Community 4"
Cohesion: 0.09
Nodes (21): ObjectStore, batch_hash(), BronzeManifest, row_hash(), _skipped(), _to_parquet(), write_bronze(), QualityError (+13 more)

### Community 5 - "Community 5"
Cohesion: 0.11
Nodes (7): normalize(), _atomic_write(), check_synonym(), _atomic_write(), build_store(), build_silver_store(), load_cleaning()

### Community 6 - "Community 6"
Cohesion: 0.07
Nodes (31): ConfigError, DedupConfig, dedupe(), DedupReport, DedupResult, DuplicateGroup, _field_score(), _nulls() (+23 more)

### Community 7 - "Community 7"
Cohesion: 0.05
Nodes (42): id, impact, reason, when, default, impact, reason, id (+34 more)

### Community 8 - "Community 8"
Cohesion: 0.08
Nodes (19): RawTable, _clean(), bind_rules(), transform_table(), transform_with_deadletter(), TransformResult, TransformRule, TransformStats (+11 more)

### Community 9 - "Community 9"
Cohesion: 0.15
Nodes (25): ChangeKind, ChangeImpact, classify_change(), classify_diff(), EvolutionPolicy, ch(), drift(), expected_shift() (+17 more)

### Community 10 - "Community 10"
Cohesion: 0.11
Nodes (22): _apply(), apply_rule(), Cast, _cast(), _FieldSet, _finite(), Lower, MapValues (+14 more)

### Community 11 - "Community 11"
Cohesion: 0.16
Nodes (24): SchemaRegistry, col(), drift(), engine(), fp(), reg(), test_accept_and_baseline(), test_change_increments_version() (+16 more)

### Community 12 - "Community 12"
Cohesion: 0.16
Nodes (20): _cell(), _dig(), fetch_all(), get(), Pagination, _retry_after(), _Retryable, _safe_url() (+12 more)

### Community 13 - "Community 13"
Cohesion: 0.12
Nodes (15): ColumnProfile, names(), non_reorder(), of(), profiles(), test_added_removed_symmetry_after_matching(), test_added_removed_symmetry_without_renames(), test_attribute_changes_of_same_name_columns() (+7 more)

### Community 14 - "Community 14"
Cohesion: 0.10
Nodes (15): main(), scan(), list_buckets(), main(), postgres_ready(), _sign(), _sign(), hashes() (+7 more)

### Community 15 - "Community 15"
Cohesion: 0.07
Nodes (34): minio, MINIO_ROOT_PASSWORD, MINIO_ROOT_USER, POSTGRES_DB, POSTGRES_PASSWORD, POSTGRES_USER, interval, retries (+26 more)

### Community 16 - "Community 16"
Cohesion: 0.10
Nodes (17): Backlog, cmd_export_md(), cmd_hook_session(), cmd_hook_stop(), cmd_hypo(), cmd_hypo_add(), cmd_set(), cmd_status() (+9 more)

### Community 17 - "Community 17"
Cohesion: 0.11
Nodes (28): _date_hint_from(), profile_column(), make(), profile_table(), run(), hint(), t(), table() (+20 more)

### Community 18 - "Community 18"
Cohesion: 0.10
Nodes (13): EventBus, SchemaChanged, EvolutionAction, build_report(), _cell(), _column_text(), EvolutionReport, _kind_text() (+5 more)

### Community 19 - "Community 19"
Cohesion: 0.12
Nodes (23): CleaningPolicy, render_log(), cleaned(), one(), run(), test_bad_cleaning_config(), test_bad_rows_go_to_deadletter_with_invariant(), test_clean_input_gives_empty_log() (+15 more)

### Community 20 - "Community 20"
Cohesion: 0.10
Nodes (8): _CountingProvider, run_llm_mode(), BudgetLedger, build_mapper(), is_unresolved(), LLMMapper, LLMProvider, _default_mapper()

### Community 21 - "Community 21"
Cohesion: 0.22
Nodes (18): _assert_no_leak(), _claude(), _echo(), FakeClient, _mapper(), _profile(), test_budget_accumulates_and_blocks(), test_budget_unreadable_means_exhausted_and_new_month_resets() (+10 more)

### Community 22 - "Community 22"
Cohesion: 0.12
Nodes (8): ingest_with_deadletter(), IngestSummary, Reject, RetryPolicy, _to_reject(), write_rejects(), BadRow, read_file_with_rejects()

### Community 23 - "Community 23"
Cohesion: 0.12
Nodes (15): SemanticDictionary, merge_learned(), MergeReport, _csv_header(), test_candidates_by_token(), test_duplicate_synonym_across_fields_is_rejected(), test_duplicate_synonym_inside_one_field_is_folded(), test_empty_synonym_rejected() (+7 more)

### Community 24 - "Community 24"
Cohesion: 0.13
Nodes (21): map_column(), RulesConfig, _type_compatible(), _header(), test_drift_fixture_columns(), test_empty_inputs(), test_fuzzy_abbreviations_above_threshold(), test_invalid_config_rejected() (+13 more)

### Community 25 - "Community 25"
Cohesion: 0.13
Nodes (12): bind_run_id(), configure_logging(), get_logger(), make_bronze_asset(), _bronze(), make_bronze_assets(), _clean(), _lines() (+4 more)

### Community 26 - "Community 26"
Cohesion: 0.11
Nodes (13): _attribute_changes(), _close(), DiffConfig, dist_similarity(), _iou(), _is_abbrev(), name_similarity(), RenameCandidate (+5 more)

### Community 27 - "Community 27"
Cohesion: 0.16
Nodes (21): _drift(), refused(), rule(), run(), test_booleans_fr_en(), test_bounds_refused(), test_data_fields_are_data_never_code(), test_dates() (+13 more)

### Community 28 - "Community 28"
Cohesion: 0.18
Nodes (16): render_markdown(), drift(), engine(), make_bus(), policy(), reg(), run(), test_changed_schema_requires_matching_previous() (+8 more)

### Community 29 - "Community 29"
Cohesion: 0.12
Nodes (10): _columns_json(), MappingProposal, MappingRequest, MappingResponse, render_prompt(), _retryable(), _safe_json(), SimProvider (+2 more)

### Community 30 - "Community 30"
Cohesion: 0.11
Nodes (11): ColumnChange, SchemaDiff, ClassifiedChange, Conditions, DefaultRule, DiffImpact, _facts(), Rule (+3 more)

### Community 31 - "Community 31"
Cohesion: 0.16
Nodes (19): read_back(), silver_chain(), test_bad_arguments(), test_bad_columns_refused(), test_crash_between_parquet_and_manifest(), test_different_batch_new_file(), test_drift_chain_v3_identical_to_v1(), test_empty_batch_is_valid() (+11 more)

### Community 32 - "Community 32"
Cohesion: 0.17
Nodes (15): _failure(), _local(), _meta(), _ran(), _read(), _run(), _silver_files(), _spec() (+7 more)

### Community 33 - "Community 33"
Cohesion: 0.19
Nodes (16): diff_schemas(), drift(), expected(), _ops(), test_column_profile_list_input(), test_deterministic_and_input_order_independent(), test_duplicate_column_rejected(), test_empty_schemas() (+8 more)

### Community 34 - "Community 34"
Cohesion: 0.10
Nodes (21): enabledPlugins, ponytail@ponytail, ui-ux-pro-max@ui-ux-pro-max-skill, extraKnownMarketplaces, ponytail, ui-ux-pro-max-skill, hooks, PostToolUse (+13 more)

### Community 35 - "Community 35"
Cohesion: 0.13
Nodes (14): dates(), floats(), gen(), ints(), pick(), _same_values(), seq(), table() (+6 more)

### Community 36 - "Community 36"
Cohesion: 0.14
Nodes (7): _check_ident(), _dump(), extract_incremental(), _parser(), _to_str(), WatermarkStore, test_unreachable_database_error_hides_secret()

### Community 37 - "Community 37"
Cohesion: 0.19
Nodes (11): AdpError, SourceSpecError, load_registry(), load_source(), test_auth_ref_optional(), test_invalid_yaml_raises_clear_error(), test_missing_file_raises(), test_registry_loads_and_rejects_duplicates() (+3 more)

### Community 38 - "Community 38"
Cohesion: 0.16
Nodes (15): LLMError, assert_no_raw_values(), _check(), parse_response(), test_llm_mode_total_failure_not_measured(), propose(), _answer(), _req() (+7 more)

### Community 39 - "Community 39"
Cohesion: 0.15
Nodes (7): _Default, Move, _parse(), plan_archive(), RetentionPolicy, _SourceRule, test_source_override()

### Community 40 - "Community 40"
Cohesion: 0.20
Nodes (9): engine(), _insert(), _pg_engine(), _store(), table(), test_error_mid_read_keeps_watermark(), test_first_read_all_then_nothing_then_five(), test_invalid_identifiers() (+1 more)

### Community 41 - "Community 41"
Cohesion: 0.16
Nodes (8): apply_archive(), ArchiveResult, LocalStore, test_destination_differs_loses_nothing(), test_rejects_travel_with_batch(), test_resume_after_crash_before_manifest(), test_source_and_destination_exist(), test_store_list_ignores_temp_and_prefix()

### Community 42 - "Community 42"
Cohesion: 0.14
Nodes (8): _Answer, build_request(), ColumnHint, GlossaryEntry, GlossaryIndex, _Item, LLMConfig, test_retrieve_deterministic_and_bounded()

### Community 43 - "Community 43"
Cohesion: 0.11
Nodes (17): runs-on, steps, strategy, jobs, ci, python-version, name, on (+9 more)

### Community 44 - "Community 44"
Cohesion: 0.15
Nodes (7): Settings, _clean_env(), _load_checker(), test_check_secrets_flags_and_passes(), test_defaults(), test_env_override(), test_secrets_masked()

### Community 45 - "Community 45"
Cohesion: 0.14
Nodes (8): SchemaError, _found_in_log(), GateResult, _known(), _report_key(), run_schema_gate(), _columns(), SchemaFingerprint

### Community 46 - "Community 46"
Cohesion: 0.20
Nodes (6): _assert_no_drift(), fp(), test_0002_backfills_accepted_versions(), test_revision_matches_model_sqlite(), test_upgrade_postgres(), _upgrade()

### Community 47 - "Community 47"
Cohesion: 0.15
Nodes (7): load_run_dictionary(), make_silver_asset(), _silver(), make_silver_assets(), _metadata(), run_silver_chain(), SilverChainResult

### Community 48 - "Community 48"
Cohesion: 0.25
Nodes (9): summarize(), _archive(), _line(), main(), _queue(), _review_approve(), _review_list(), _review_reject() (+1 more)

### Community 49 - "Community 49"
Cohesion: 0.16
Nodes (12): load_retention(), _batch(), _snapshot(), test_cli_bad_config_exit_code(), test_cli_bad_date(), test_cli_summary_by_source(), test_dry_run_changes_nothing_and_matches_real(), test_invalid_config() (+4 more)

### Community 50 - "Community 50"
Cohesion: 0.19
Nodes (9): TransformError, _check(), _explain(), FieldTransform, load_field_transforms(), parse_field_transforms(), test_bind_rules_requires_a_transform_per_target(), test_example_config_covers_dictionary() (+1 more)

### Community 51 - "Community 51"
Cohesion: 0.24
Nodes (11): load_policy(), _permissive_policy(), policy(), test_empty_rule_list_rejected(), test_invalid_policy_rejected(), test_missing_file(), test_modified_policy_changes_result(), test_non_mapping_rejected() (+3 more)

### Community 52 - "Community 52"
Cohesion: 0.19
Nodes (3): upgrade(), upgrade(), upgrade()

### Community 53 - "Community 53"
Cohesion: 0.16
Nodes (5): build_provider(), ClaudeProvider, LocalProvider, test_build_provider_without_key_is_local(), test_real_claude_shape_only()

### Community 54 - "Community 54"
Cohesion: 0.31
Nodes (13): _count(), _csv(), _go(), test_corrupt_data_not_retried(), test_invariant_ok_plus_rejected_equals_read(), test_jsonl_invalid_line(), test_logs_have_no_row_values(), test_replay_no_duplicates() (+5 more)

### Community 55 - "Community 55"
Cohesion: 0.21
Nodes (6): _bump(), classify_bump(), _load_columns(), SchemaColumn, _utc(), test_classify_bump()

### Community 56 - "Community 56"
Cohesion: 0.18
Nodes (7): confidence_band(), _profile_hints(), _shape(), shape_pattern(), trace_record(), test_shape_pattern_anonymizes(), test_trace_record_and_bands()

### Community 57 - "Community 57"
Cohesion: 0.18
Nodes (3): CanonicalField, _best(), _similarity()

### Community 58 - "Community 58"
Cohesion: 0.26
Nodes (5): ChangeRecord, SchemaChange, rows_of(), test_record_changes_round_trip(), test_record_and_read_changes()

### Community 59 - "Community 59"
Cohesion: 0.24
Nodes (4): _check_source(), SchemaVersion, _to_version(), test_accept_unknown_version()

### Community 60 - "Community 60"
Cohesion: 0.18
Nodes (10): entry, files, id, language, name, pass_filenames, types, repos (+2 more)

### Community 62 - "Community 62"
Cohesion: 0.25
Nodes (8): validate_rules(), _table(), test_clean_table_writes_nothing(), test_hostile_text_in_any_code_field_is_refused_without_execution(), test_missing_source_column_fails_the_batch(), test_per_row_errors_go_to_dead_letter_without_values(), test_short_rows_are_nulls_not_crashes(), test_threshold_and_idempotent_rejects()

### Community 63 - "Community 63"
Cohesion: 0.35
Nodes (10): _files(), test_crash_between_parquet_and_manifest(), put_if_absent(), test_different_batch_new_file(), test_manifest_content(), test_replay_is_noop_and_file_untouched(), test_reserved_column_rejected(), test_row_hash_stable_and_sensitive() (+2 more)

### Community 64 - "Community 64"
Cohesion: 0.20
Nodes (4): BronzeConfig, landing_dir(), make_landing_sensor(), _sensor()

### Community 66 - "Community 66"
Cohesion: 0.24
Nodes (4): build_definitions(), defs(), test_definitions_load(), test_definitions_include_schema_check()

### Community 67 - "Community 67"
Cohesion: 0.27
Nodes (6): load_dictionary(), dico(), test_invalid_yaml_raises_config_error_with_file(), test_loader_uses_safe_load(), test_non_mapping_and_missing_file(), _write()

### Community 68 - "Community 68"
Cohesion: 0.51
Nodes (8): _meta(), _run(), _spec(), test_corrupt_file_goes_to_deadletter(), test_materialize_and_replay(), test_non_file_sources_have_no_asset(), test_sensor_path_runs_asset(), test_sensor_skip_then_run_then_no_retrigger()

### Community 70 - "Community 70"
Cohesion: 0.28
Nodes (5): _adrs(), test_adr_has_required_sections(), test_architecture_has_mermaid_flowchart(), test_architecture_links_to_existing_adrs(), test_at_least_six_numbered_adrs()

### Community 72 - "Community 72"
Cohesion: 0.28
Nodes (4): make_schema_check_asset(), _check(), make_schema_check_assets(), SourceSpec

### Community 73 - "Community 73"
Cohesion: 0.25
Nodes (7): fields, description, domain, name, synonyms, type, unit

### Community 74 - "Community 74"
Cohesion: 0.25
Nodes (7): fields, steps, target, dayfirst, op, to, values

### Community 75 - "Community 75"
Cohesion: 0.29
Nodes (6): default, archive_prefix, retention_days, sources, world_bank_gdp, retention_days

### Community 76 - "Community 76"
Cohesion: 0.29
Nodes (6): format, frequency, name, primary_key, type, uri

### Community 77 - "Community 77"
Cohesion: 0.29
Nodes (6): format, frequency, name, primary_key, type, uri

### Community 78 - "Community 78"
Cohesion: 0.29
Nodes (6): format, frequency, name, primary_key, type, uri

### Community 79 - "Community 79"
Cohesion: 0.43
Nodes (3): reply(), FakeMessages, _prompt_payload()

### Community 80 - "Community 80"
Cohesion: 0.27
Nodes (3): map_columns(), test_deterministic_and_sorted(), store()

### Community 83 - "Community 83"
Cohesion: 0.40
Nodes (4): fields, accent_key, case, target

### Community 84 - "Community 84"
Cohesion: 0.40
Nodes (5): max_concurrent_runs, run_coordinator, class, config, module

## Knowledge Gaps
- **1 isolated node(s):** `adp`
  These have ≤1 connection - possible missing edges. (Counts symbols only; 647 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **17 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `ColumnProfile` connect `Community 13` to `Community 0`, `Community 33`, `Community 5`, `Community 42`, `Community 45`, `Community 80`, `Community 17`, `Community 20`, `Community 21`, `Community 56`, `Community 24`, `Community 26`?**
  _High betweenness centrality (0.031) - this node is a cross-community bridge._
- **Are the 44 inferred relationships involving `SemanticDictionary` (e.g. with `ReviewQueue` and `_cascade()`) actually correct?**
  _`SemanticDictionary` has 44 INFERRED edges - model-reasoned connections that need verification._
- **What connects `adp` to the rest of the system?**
  _1 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Community 0` be split into smaller, more focused modules?**
  _Cohesion score 0.05284147557328016 - nodes in this community are weakly interconnected._
- **Why does `SemanticDictionary` connect `Community 23` to `Community 0`, `Community 1`, `Community 3`, `Community 4`, `Community 5`, `Community 8`, `Community 10`, `Community 20`, `Community 24`, `Community 31`, `Community 32`, `Community 42`, `Community 47`, `Community 50`, `Community 53`, `Community 57`, `Community 62`, `Community 67`, `Community 80`?**
  _High betweenness centrality (0.026) - this node is a cross-community bridge._
- **Are the 44 inferred relationships involving `SchemaRegistry` (e.g. with `_found_in_log()` and `_known()`) actually correct?**
  _`SchemaRegistry` has 44 INFERRED edges - model-reasoned connections that need verification._
- **Should `Community 1` be split into smaller, more focused modules?**
  _Cohesion score 0.050949367088607596 - nodes in this community are weakly interconnected._