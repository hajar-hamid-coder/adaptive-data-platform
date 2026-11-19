// Types aligned with the pydantic models / routers of src/adp/api (read-only API).

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

/** GET /runs item (one per quality-scored run). */
export interface Run {
  source_name: string;
  run_id: string;
  schema_version: number | null;
  score: number | null;
  rows_total: number;
  rows_failed: number;
  created_at: string;
}

/** GET /quality/scores item (StoredScore, batch scope). */
export interface QualityScore extends Run {
  id: number;
  scope: "batch" | "column";
  column_name: string | null;
  completeness: number | null;
  validity: number | null;
  uniqueness: number | null;
  freshness: number | null;
  config_hash: string;
}

export interface ColumnScore {
  column: string;
  required: boolean;
  completeness: number | null;
  validity: number | null;
  uniqueness: number | null;
  freshness: number | null;
  score: number | null;
  rows_failed: number;
}

export interface FailedRule {
  rule_id: string;
  type: string;
  severity: "error" | "warn" | string;
  level: string;
  column: string | null;
  evaluated: number;
  failed_count: number;
  failed_rows: number[];
  message: string;
}

/** GET /quality/reports/{run_id} (JSON written by the promotion gate). */
export interface QualityReport {
  run_id: string;
  source: string;
  generated_at: string;
  summary: {
    status: "scored" | "n/a";
    score: number | null;
    rows_total: number;
    rows_failed: number;
    ok: boolean;
    dimensions: Record<"completeness" | "validity" | "uniqueness" | "freshness", number | null>;
  };
  columns: ColumnScore[];
  failed_rules: FailedRule[];
  quarantine: {
    level: string;
    alert: boolean;
    failed_rate: number;
    batch_reasons: string[];
    quarantined_rows: number;
  } | null;
}

/** GET /quality/quarantine/{source} item (indices and reasons only, never raw values). */
export interface QuarantineRow {
  batch_id: string;
  row: number;
  status: string;
  batch_level: string;
  reasons: { rule_id: string; severity: string }[];
}

export interface SchemaColumn {
  name: string;
  type: string;
  nullable: boolean;
}

/** GET /schemas/{source} item. */
export interface SchemaVersion {
  source_name: string;
  version: number;
  semver: string;
  fingerprint: string;
  columns: SchemaColumn[];
  created_at: string;
  accepted_at: string | null;
}

/** GET /schemas/{source}/diff item (`detail` carries action/impact/rule_id/... when known). */
export interface SchemaChange {
  change_type: string;
  column_name: string | null;
  detail: Record<string, unknown>;
}

/** GET /catalog item. */
export interface CatalogProduct {
  name: string;
  version: string;
  owner: string;
  criticality: string;
  sla_hours: number;
  description: string;
  n_columns: number;
}
