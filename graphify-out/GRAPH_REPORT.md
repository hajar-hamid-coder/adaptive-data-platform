# Graph Report - adaptive-data-platform  (2026-10-31)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 206 nodes · 286 edges · 13 communities (10 shown, 3 thin omitted)
- Extraction: 96% EXTRACTED · 4% INFERRED · 0% AMBIGUOUS · INFERRED: 11 edges (avg confidence: 0.93)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `2c0d9500`
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
- Community 11
- Community 12

## God Nodes (most connected - your core abstractions)
1. `main()` - 14 edges
2. `Backlog` - 9 edges
3. `AdpError` - 8 edges
4. `hooks` - 8 edges
5. `Settings` - 7 edges
6. `get_logger()` - 7 edges
7. `list_buckets()` - 7 edges
8. `configure_logging()` - 7 edges
9. `minio` - 7 edges
10. `ConfigError` - 6 edges

## Surprising Connections (you probably didn't know these)
- `test_defaults()` --uses--> `Settings`  [INFERRED]
  tests/test_config.py → src/adp/config.py
- `test_env_override()` --uses--> `Settings`  [INFERRED]
  tests/test_config.py → src/adp/config.py
- `test_secrets_masked()` --uses--> `Settings`  [INFERRED]
  tests/test_config.py → src/adp/config.py
- `test_error_hierarchy()` --uses--> `AdpError`  [INFERRED]
  tests/test_logging.py → src/adp/errors.py
- `test_error_hierarchy()` --uses--> `ConfigError`  [INFERRED]
  tests/test_logging.py → src/adp/errors.py

## Import Cycles
- None detected.

## Communities (13 total, 3 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.10
Nodes (6): main(), scan(), list_buckets(), main(), postgres_ready(), _sign()

### Community 1 - "Community 1"
Cohesion: 0.14
Nodes (13): Backlog, cmd_export_md(), cmd_hook_session(), cmd_hook_stop(), cmd_hypo(), cmd_hypo_add(), cmd_set(), cmd_status() (+5 more)

### Community 2 - "Community 2"
Cohesion: 0.12
Nodes (9): get_settings(), Settings, _clean_env(), _load_checker(), test_check_secrets_flags_and_passes(), test_defaults(), test_env_override(), test_get_settings_cached() (+1 more)

### Community 3 - "Community 3"
Cohesion: 0.17
Nodes (8): bind_run_id(), configure_logging(), get_logger(), _clean(), _lines(), test_json_lines_have_run_id_level_event(), test_level_filtering(), test_run_id_explicit_and_persists()

### Community 4 - "Community 4"
Cohesion: 0.10
Nodes (21): enabledPlugins, ponytail@ponytail, ui-ux-pro-max@ui-ux-pro-max-skill, extraKnownMarketplaces, ponytail, ui-ux-pro-max-skill, hooks, PostToolUse (+13 more)

### Community 5 - "Community 5"
Cohesion: 0.10
Nodes (20): minio, MINIO_ROOT_PASSWORD, MINIO_ROOT_USER, condition, entrypoint, environment, image, depends_on (+12 more)

### Community 6 - "Community 6"
Cohesion: 0.11
Nodes (17): runs-on, steps, strategy, jobs, ci, python-version, name, on (+9 more)

### Community 7 - "Community 7"
Cohesion: 0.18
Nodes (14): POSTGRES_DB, POSTGRES_PASSWORD, POSTGRES_USER, interval, retries, test, timeout, healthcheck (+6 more)

### Community 8 - "Community 8"
Cohesion: 0.26
Nodes (6): AdpError, ConfigError, IngestionError, QualityError, SchemaError, test_error_hierarchy()

### Community 9 - "Community 9"
Cohesion: 0.18
Nodes (10): entry, files, id, language, name, pass_filenames, types, repos (+2 more)

## Knowledge Gaps
- **1 isolated node(s):** `adp`
  These have ≤1 connection - possible missing edges. (Counts symbols only; 90 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **3 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `test_error_hierarchy()` connect `Community 8` to `Community 3`?**
  _High betweenness centrality (0.020) - this node is a cross-community bridge._
- **Are the 2 inferred relationships involving `main()` (e.g. with `.next_task()` and `.task()`) actually correct?**
  _`main()` has 2 INFERRED edges - model-reasoned connections that need verification._
- **What connects `adp` to the rest of the system?**
  _1 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Community 0` be split into smaller, more focused modules?**
  _Cohesion score 0.09885057471264368 - nodes in this community are weakly interconnected._
- **Why does `services` connect `Community 5` to `Community 7`?**
  _High betweenness centrality (0.016) - this node is a cross-community bridge._
- **Are the 3 inferred relationships involving `Settings` (e.g. with `test_defaults()` and `test_env_override()`) actually correct?**
  _`Settings` has 3 INFERRED edges - model-reasoned connections that need verification._
- **Should `Community 1` be split into smaller, more focused modules?**
  _Cohesion score 0.14333333333333334 - nodes in this community are weakly interconnected._