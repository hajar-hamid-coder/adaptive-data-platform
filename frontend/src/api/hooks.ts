import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { ApiError, apiGet } from "./client";
import type {
  Page,
  QualityReport,
  QualityScore,
  QuarantineRow,
  Run,
  SchemaChange,
  SchemaVersion,
} from "./types";

const retry = (n: number, err: unknown): boolean => n < 1 && !(err instanceof ApiError && err.status < 500 && err.status !== 0);

export const useRuns = () =>
  useQuery({ queryKey: ["runs"], queryFn: ({ signal }) => apiGet<Page<Run>>("/runs", { limit: 200 }, signal), retry });

export const useScores = (source: string | undefined) =>
  useQuery({
    queryKey: ["scores", source],
    queryFn: ({ signal }) => apiGet<Page<QualityScore>>("/quality/scores", { source, limit: 200 }, signal),
    enabled: !!source,
    retry,
  });

export const useReport = (runId: string | undefined) =>
  useQuery({
    queryKey: ["report", runId],
    queryFn: ({ signal }) => apiGet<QualityReport>(`/quality/reports/${encodeURIComponent(runId ?? "")}`, {}, signal),
    enabled: !!runId,
    retry,
  });

export const useQuarantine = (source: string | undefined, offset: number, limit: number) =>
  useQuery({
    queryKey: ["quarantine", source, offset, limit],
    queryFn: ({ signal }) =>
      apiGet<Page<QuarantineRow>>(`/quality/quarantine/${encodeURIComponent(source ?? "")}`, { limit, offset }, signal),
    enabled: !!source,
    placeholderData: keepPreviousData,
    retry,
  });

export const useSchemaVersions = (source: string | undefined) =>
  useQuery({
    queryKey: ["schemas", source],
    queryFn: ({ signal }) => apiGet<Page<SchemaVersion>>(`/schemas/${encodeURIComponent(source ?? "")}`, { limit: 200 }, signal),
    enabled: !!source,
    retry,
  });

export const useSchemaDiff = (source: string | undefined, from: number | undefined, to: number | undefined) =>
  useQuery({
    queryKey: ["diff", source, from, to],
    queryFn: ({ signal }) =>
      apiGet<Page<SchemaChange>>(`/schemas/${encodeURIComponent(source ?? "")}/diff`, { from, to, limit: 200 }, signal),
    enabled: !!source && from !== undefined && to !== undefined,
    retry,
  });

/** Sources are not listed by the API: deduced from the runs (documented limit). */
export const useSources = () =>
  useQuery({
    queryKey: ["runs"],
    queryFn: ({ signal }) => apiGet<Page<Run>>("/runs", { limit: 200 }, signal),
    retry,
    select: (p) => [...new Set(p.items.map((r) => r.source_name))].sort(),
  });
