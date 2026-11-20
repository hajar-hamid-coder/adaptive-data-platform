import { describe, expect, it } from "vitest";
import { ApiError, apiGet, errorMessage } from "../api/client";
import { mockApi, status } from "./helpers";

describe("api client", () => {
  it("calls the configurable base with query params and never sends an API key", async () => {
    const { fn, calls } = mockApi({ "/runs": () => ({ items: [], total: 0, limit: 5, offset: 0 }) });
    const r = await apiGet<{ total: number }>("/runs", { limit: 5, source: undefined, offset: 0 });
    expect(r.total).toBe(0);
    expect(calls[0]?.pathname).toBe("/api/runs");
    expect(calls[0]?.search).toBe("?limit=5&offset=0");
    const init = fn.mock.calls[0]?.[1] as RequestInit;
    expect(JSON.stringify(init.headers)).not.toMatch(/api-key/i);
  });

  it("raises a typed ApiError on HTTP and network failures", async () => {
    mockApi({ "/runs": () => status(401) });
    await expect(apiGet("/runs")).rejects.toMatchObject({ status: 401 });
    mockApi({ "/x": () => status(404) });
    await expect(apiGet("/x")).rejects.toBeInstanceOf(ApiError);
    const err = await apiGet("/x").catch((e: unknown) => e);
    expect((err as ApiError).status).toBe(404);
    // network failure
    globalThis.fetch = (() => Promise.reject(new TypeError("down"))) as typeof fetch;
    await expect(apiGet("/runs")).rejects.toMatchObject({ status: 0 });
  });

  it("maps errors to actionable messages without technical detail", () => {
    expect(errorMessage(new ApiError(401))).toMatch(/Clé d'API invalide ou absente/);
    expect(errorMessage(new ApiError(0))).toMatch(/joindre l'API/);
    expect(errorMessage(new ApiError(500))).not.toMatch(/500/);
  });
});
