import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import { routes } from "../App";

type Handler = (url: URL) => unknown | Response;

/** fetch mock: first matching prefix wins; a Response is returned as is, anything else as JSON 200. */
export function mockApi(handlers: Record<string, Handler>) {
  const calls: URL[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    calls.push(url);
    const key = Object.keys(handlers).find((k) => url.pathname.replace(/^\/api/, "") === k || (k.endsWith("*") && url.pathname.replace(/^\/api/, "").startsWith(k.slice(0, -1))));
    if (!key) return new Response("{}", { status: 404 });
    const out = handlers[key]!(url);
    return out instanceof Response ? out : new Response(JSON.stringify(out), { status: 200, headers: { "content-type": "application/json" } });
  });
  vi.stubGlobal("fetch", fn);
  return { fn, calls };
}

export const status = (code: number): Response => new Response("{}", { status: code });

export function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>{routes}</MemoryRouter>
    </QueryClientProvider>,
  );
}
