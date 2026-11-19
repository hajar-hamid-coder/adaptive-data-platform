import { act, render, renderHook, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ScoreBar } from "../components/ui";
import { Fill, MotionProvider, Reveal, useReducedMotion } from "../motion";
import { Layout } from "../components/Layout";
import { MemoryRouter } from "react-router-dom";

/** matchMedia stub with a controllable `prefers-reduced-motion` and change events. */
function stubMedia(reduce: boolean) {
  let matches = reduce;
  const listeners = new Set<() => void>();
  vi.stubGlobal("matchMedia", (q: string) => ({
    get matches() {
      return q.includes("reduce") ? matches : false;
    },
    media: q,
    addEventListener: (_: string, f: () => void) => listeners.add(f),
    removeEventListener: (_: string, f: () => void) => listeners.delete(f),
  }));
  return (v: boolean) => {
    matches = v;
    listeners.forEach((f) => f());
  };
}

describe("useReducedMotion", () => {
  it("reflects the media query and reacts to changes", () => {
    const set = stubMedia(false);
    const { result } = renderHook(() => useReducedMotion());
    expect(result.current).toBe(false);
    act(() => set(true));
    expect(result.current).toBe(true);
    act(() => set(false));
    expect(result.current).toBe(false);
  });

  it("does not animate when matchMedia is unavailable", () => {
    vi.stubGlobal("matchMedia", undefined);
    expect(renderHook(() => useReducedMotion()).result.current).toBe(true);
  });
});

describe("animated components", () => {
  it("reduced motion: final state at once, no transform, no opacity 0, no clip", () => {
    stubMedia(true);
    render(
      <MotionProvider>
        <Reveal className="r">contenu</Reveal>
        <ScoreBar value={81} />
      </MotionProvider>,
    );
    const r = screen.getByText("contenu");
    expect(r.style.transform).toBe("none");
    expect(r.style.opacity).toBe("1");
    expect(document.querySelector<HTMLElement>(".fill")!.style.clipPath).not.toContain("100%");
    expect(screen.getByRole("img", { name: "Score 81,0 sur 100" })).toBeInTheDocument();
    expect(screen.getByText("81,0")).toBeInTheDocument();
  });

  it("motion allowed: starts faded and shifted, bar clipped", () => {
    stubMedia(false);
    render(
      <MotionProvider>
        <Reveal>contenu</Reveal>
        <Fill percent={50} />
      </MotionProvider>,
    );
    const r = screen.getByText("contenu");
    expect(r.style.opacity).toBe("0");
    expect(r.style.transform).toContain("translateY(6px)");
    expect(document.querySelector<HTMLElement>(".fill")!.style.clipPath).toContain("100%");
  });

  it("page transition keeps main focusable and focus stable", () => {
    stubMedia(false);
    render(
      <MemoryRouter>
        <MotionProvider>
          <Layout />
        </MotionProvider>
      </MemoryRouter>,
    );
    const main = screen.getByRole("main");
    main.focus();
    expect(main).toHaveFocus();
  });
});
