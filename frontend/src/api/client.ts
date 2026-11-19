/** Typed, read-only API client. The base is configurable; the API key is NEVER handled here:
 * the dev proxy (vite.config.ts) or the production reverse proxy injects the key header. */

export const API_BASE: string = import.meta.env.VITE_API_BASE ?? "/api";

export class ApiError extends Error {
  constructor(readonly status: number) {
    super(`API error ${status}`);
    this.name = "ApiError";
  }
}

export type Params = Record<string, string | number | undefined>;

export async function apiGet<T>(path: string, params: Params = {}, signal?: AbortSignal): Promise<T> {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined) qs.set(k, String(v));
  const q = qs.toString();
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}${q ? `?${q}` : ""}`, { headers: { Accept: "application/json" }, signal });
  } catch {
    throw new ApiError(0);
  }
  if (!res.ok) throw new ApiError(res.status);
  return (await res.json()) as T;
}

/** User-facing message: what happened and what to do, without technical detail. */
export function errorMessage(err: unknown): string {
  const status = err instanceof ApiError ? err.status : 0;
  if (status === 401) return "Clé d'API invalide ou absente. Vérifiez la configuration du proxy.";
  if (status === 404) return "Ressource introuvable.";
  if (status === 0) return "Impossible de joindre l'API. Vérifiez qu'elle est démarrée.";
  return "L'API a répondu par une erreur. Réessayez dans un instant.";
}
