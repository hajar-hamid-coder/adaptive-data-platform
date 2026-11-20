import type { Run } from "./api/types";

const nf = new Intl.NumberFormat("fr-FR", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
export const fmtScore = (v: number | null): string => (v === null ? "n/a" : nf.format(v));
export const fmtInt = (v: number): string => new Intl.NumberFormat("fr-FR").format(v);
export const hhmm = (iso: string): string => iso.slice(11, 16);
export const isoDate = (iso: string): string => iso.slice(0, 10);
export const isoMinute = (iso: string): string => `${iso.slice(0, 16)}Z`;

export type Tone = "ok" | "warn" | "error" | "info" | "quarantine";

/** DESIGN.md thresholds: >= 90 ok (solid), 75-90 warn (hatched), < 75 error. */
export const scoreTone = (s: number | null): Tone => (s === null ? "info" : s >= 90 ? "ok" : s >= 75 ? "warn" : "error");

/** Latest run of each source (input is newest first, as served). */
export function latestBySource(runs: Run[]): Run[] {
  const seen = new Map<string, Run>();
  for (const r of runs) if (!seen.has(r.source_name)) seen.set(r.source_name, r);
  return [...seen.values()].sort((a, b) => a.source_name.localeCompare(b.source_name));
}

/** Status deduced from the score row only (no persisted run journal): failed rows = isolated. */
export const runStatus = (r: Run): { tone: Tone; label: string } =>
  r.rows_failed > 0 ? { tone: "quarantine", label: "Quarantaine partielle" } : { tone: "ok", label: "Promu" };

export const IMPACT: Record<string, { tone: Tone; label: string; rank: number }> = {
  compatible: { tone: "ok", label: "Compatible", rank: 0 },
  review: { tone: "warn", label: "À revoir", rank: 1 },
  blocking: { tone: "error", label: "Bloquant", rank: 2 },
};

export function impactOf(detail: Record<string, unknown>): (typeof IMPACT)[string] {
  const key = String(detail["impact"] ?? "").toLowerCase();
  return IMPACT[key] ?? { tone: "info", label: "Non classé", rank: 1 };
}

export const SIGNS: Record<string, string> = {
  add_column: "+",
  drop_column: "−",
  rename_column: "~",
  type_change: "≠",
  format_change: "≠",
  nullability_change: "≠",
  reorder: "↕",
};

export const SEVERITY: Record<string, { tone: Tone; label: string }> = {
  error: { tone: "error", label: "Bloquante" },
  warn: { tone: "warn", label: "Avertissement" },
  warning: { tone: "warn", label: "Avertissement" },
  info: { tone: "info", label: "Information" },
};

export const QSTATUS: Record<string, { tone: Tone; label: string }> = {
  quarantined: { tone: "quarantine", label: "En quarantaine" },
  replayed: { tone: "info", label: "Rejouée" },
  released: { tone: "ok", label: "Libérée" },
};
