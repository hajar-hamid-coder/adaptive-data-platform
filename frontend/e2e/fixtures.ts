import AxeBuilder from "@axe-core/playwright";
import { expect, test as base } from "@playwright/test";

/** baseURL comes from the stack started in global setup (free ports, known only at run time). */
export const test = base.extend({
  baseURL: async ({}, use) => {
    const url = process.env["ADP_E2E_BASE_URL"];
    if (!url) throw new Error("ADP_E2E_BASE_URL missing: global setup did not run");
    await use(url);
  },
});
export { expect };

export const THEMES = ["light", "dark"] as const;
export const SCREENS = [
  { name: "Pipeline", path: "/", h1: "Pipeline" },
  { name: "Qualité", path: "/qualite", h1: "Qualité des données" },
  { name: "Schémas", path: "/schemas", h1: "Schémas" },
  { name: "Revue", path: "/revue", h1: "Revue du mapping" },
] as const;

/** axe-core scan (WCAG A/AA + best practices). Returns violations of every impact for reporting. */
export async function scan(page: import("@playwright/test").Page) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "best-practice"]).analyze();
  return r.violations.map((v) => ({ id: v.id, impact: v.impact, nodes: v.nodes.length, target: v.nodes.slice(0, 3).map((n) => n.target.join(" ")) }));
}
