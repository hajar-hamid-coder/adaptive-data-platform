import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import tailwind from "../../tailwind.config";
import * as fx from "./fixtures";
import { mockApi, renderAt } from "./helpers";

describe("shell: navigation, keyboard, theme", () => {
  it("has landmarks, a skip link as first tab stop and aria-current on the active page", async () => {
    mockApi({ "/runs": () => fx.page(fx.runs), "/schemas/*": () => fx.page(fx.versions) });
    renderAt("/");
    expect(screen.getByRole("navigation", { name: "Navigation principale" })).toBeInTheDocument();
    expect(screen.getByRole("main")).toHaveAttribute("id", "contenu");
    await userEvent.tab();
    expect(screen.getByRole("link", { name: "Aller au contenu" })).toHaveFocus();
    expect(screen.getByRole("link", { name: "Pipeline" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Qualité" })).not.toHaveAttribute("aria-current");
    await screen.findByRole("table");
  });

  it("navigates with the keyboard (Enter on a nav link)", async () => {
    mockApi({ "/runs": () => fx.page(fx.runs), "/schemas/*": () => fx.page(fx.versions) });
    renderAt("/");
    screen.getByRole("link", { name: "Revue du mapping" }).focus();
    await userEvent.keyboard("{Enter}");
    expect(await screen.findByRole("heading", { name: "Revue du mapping", level: 1 })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Revue du mapping" })).toHaveAttribute("aria-current", "page");
  });

  it("toggles the theme via data-theme and stores only the theme", async () => {
    mockApi({});
    renderAt("/revue");
    const btn = screen.getByRole("button", { name: "Thème clair / sombre" });
    expect(btn).toHaveAttribute("aria-pressed", "false");
    await userEvent.click(btn);
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
    expect(btn).toHaveAttribute("aria-pressed", "true");
    expect(localStorage.getItem("adp-theme")).toBe("dark");
    expect(localStorage.length).toBe(1);
    await userEvent.click(btn);
    expect(document.documentElement).toHaveAttribute("data-theme", "light");
  });

  it("restores a stored theme", () => {
    localStorage.setItem("adp-theme", "dark");
    mockApi({});
    renderAt("/revue");
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
  });
});

describe("design system and security guards", () => {
  it("keeps the tokens identical to the mockups source of truth", () => {
    const read = (p: string) => readFileSync(new URL(p, import.meta.url), "utf8");
    expect(read("../styles/tokens.css")).toBe(read("../../mockups/tokens.css"));
  });

  it("maps tokens to Tailwind keys as in DESIGN.md 2.5", () => {
    const colors = (tailwind.theme?.extend?.colors ?? {}) as Record<string, unknown>;
    expect(colors["bg"]).toBe("var(--adp-bg)");
    expect(colors["fg-muted"]).toBe("var(--adp-text-muted)");
    expect(colors["quarantine-bg"]).toBe("var(--adp-quarantine-bg)");
    expect(tailwind.darkMode).toEqual(["selector", '[data-theme="dark"]']);
    expect(tailwind.theme?.screens).toEqual({ sm: "640px", md: "900px", lg: "1200px" });
  });

  it("never embeds an API key in the sources", () => {
    const files: string[] = [];
    const walk = (d: string) => {
      for (const n of readdirSync(d)) {
        const p = join(d, n);
        if (statSync(p).isDirectory()) walk(p);
        else if (!p.includes("/test/")) files.push(p);
      }
    };
    walk(join(process.cwd(), "src"));
    for (const f of files) expect(readFileSync(f, "utf8"), f).not.toMatch(/VITE_API_KEY|X-API-Key/i);
  });
});
