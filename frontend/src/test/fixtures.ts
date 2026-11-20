import type { Page, QualityReport, QualityScore, QuarantineRow, Run, SchemaChange, SchemaVersion } from "../api/types";

export const page = <T,>(items: T[], total = items.length, limit = 50, offset = 0): Page<T> => ({ items, total, limit, offset });

export const runs: Run[] = [
  { source_name: "world_bank_gdp", run_id: "demo_gdp1", schema_version: 1, score: 100, rows_total: 3, rows_failed: 0, created_at: "2026-11-19T23:29:07Z" },
  { source_name: "hcp_population", run_id: "demo_bad", schema_version: 2, score: 81.2, rows_total: 48, rows_failed: 8, created_at: "2026-11-19T23:28:07Z" },
  { source_name: "hcp_population", run_id: "demo_v1", schema_version: 1, score: 100, rows_total: 48, rows_failed: 0, created_at: "2026-11-19T23:20:07Z" },
];

const sc = (r: Run): QualityScore => ({ ...r, id: 1, scope: "batch", column_name: null, completeness: 100, validity: 79, uniqueness: 99.6, freshness: null, config_hash: "h" });
export const scores: QualityScore[] = runs.filter((r) => r.source_name === "hcp_population").map(sc);

export const report: QualityReport = {
  run_id: "demo_bad", source: "hcp_population", generated_at: "2026-11-19T23:28:07Z",
  summary: { status: "scored", score: 81.2, rows_total: 48, rows_failed: 8, ok: false, dimensions: { completeness: 100, validity: 79, uniqueness: 99.6, freshness: null } },
  columns: [
    { column: "unemployment_rate", required: false, completeness: 100, validity: 83, uniqueness: null, freshness: null, score: 62, rows_failed: 8 },
    { column: "region", required: true, completeness: 100, validity: 100, uniqueness: 100, freshness: null, score: 99.6, rows_failed: 0 },
  ],
  failed_rules: [{ rule_id: "unemployment_pct", type: "range", severity: "error", level: "row", column: "unemployment_rate", evaluated: 48, failed_count: 8, failed_rows: [1], message: "m" }],
  quarantine: { level: "row", alert: true, failed_rate: 0.17, batch_reasons: [], quarantined_rows: 8 },
};

export const quarantineRows = (n: number, offset = 0): QuarantineRow[] =>
  Array.from({ length: n }, (_, i) => ({ batch_id: "678ee652c42435b51c35c59bd25e1e94", row: offset + i, status: "quarantined", batch_level: "row", reasons: [{ rule_id: "unemployment_pct", severity: "error" }] }));

export const versions: SchemaVersion[] = [1, 2, 3].map((v) => ({
  source_name: "hcp_population", version: v, semver: `${v}.0.0`, fingerprint: `abcdef0${v}00000000`, columns: [{ name: "a", type: "string", nullable: false }],
  created_at: `2026-11-1${v}T08:10:00Z`, accepted_at: v < 3 ? `2026-11-1${v}T08:10:00Z` : null,
}));

export const diff: SchemaChange[] = [
  { change_type: "add_column", column_name: "canal_vente", detail: { impact: "compatible", type: "string", nullable: true } },
  { change_type: "drop_column", column_name: "taux_activite", detail: { impact: "blocking", type: "float" } },
  { change_type: "format_change", column_name: "taux_chomage", detail: { impact: "review", from: null, to: "decimal_comma", rule_id: "format_change" } },
];
