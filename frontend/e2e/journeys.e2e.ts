import { expect, test } from "./fixtures";

const NAV = ["Pipeline", "Qualité", "Schémas", "Revue du mapping"];

test("navigation with the mouse across the 4 screens", async ({ page }) => {
  await page.goto("/");
  const nav = page.getByRole("navigation", { name: "Navigation principale" });
  for (const [label, h1] of [["Qualité", "Qualité des données"], ["Schémas", "Schémas"], ["Revue du mapping", "Revue du mapping"], ["Pipeline", "Pipeline"]] as const) {
    await nav.getByRole("link", { name: label }).click();
    await expect(page.getByRole("heading", { level: 1, name: h1 })).toBeVisible();
    await expect(nav.getByRole("link", { name: label })).toHaveAttribute("aria-current", "page");
  }
});

test("navigation with the keyboard only (skip link, Tab, Enter)", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "Pipeline" })).toBeVisible();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Aller au contenu" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("main#contenu")).toBeFocused();
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "Pipeline" })).toBeVisible();
  await page.keyboard.press("Tab"); // skip link
  for (const label of NAV.slice(0, 2)) {
    await page.keyboard.press("Tab");
    await expect(page.getByRole("link", { name: label })).toBeFocused();
  }
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { level: 1, name: "Qualité des données" })).toBeVisible();
  await expect(page).toHaveURL(/\/qualite$/);
});

test("Pipeline shows the demo sources and scores", async ({ page }) => {
  await page.goto("/");
  const hcp = page.getByRole("row").filter({ has: page.getByRole("rowheader", { name: "hcp_population" }) });
  await expect(hcp).toContainText("Quarantaine partielle");
  await expect(hcp).toContainText("lignes isolées");
  await expect(hcp.getByRole("img", { name: /^Score .* sur 100$/ })).toBeVisible();
  const wb = page.getByRole("row").filter({ has: page.getByRole("rowheader", { name: "world_bank_gdp" }) });
  await expect(wb).toContainText("Promu");
  await expect(page.getByRole("region", { name: "Indicateurs clés" })).toContainText("Sources actives");
});

test("Quality: global score, failing rules, real quarantine without raw values", async ({ page }) => {
  await page.goto("/qualite");
  await expect(page.getByText(/Score global \(/)).toBeVisible();
  await expect(page.getByRole("table", { name: /Règles en échec du run/ })).toBeVisible();
  await page.getByLabel("Source").selectOption("hcp_population");
  const q = page.getByRole("region", { name: "Lignes en quarantaine" });
  await expect(q.getByRole("row")).toHaveCount(9); // header + the 8 demo rows
  await expect(page.getByRole("navigation", { name: "Pagination de la quarantaine" })).toContainText(/^1 à 8 sur 8/);
  await expect(page.getByRole("heading", { name: "Quarantaine" }).locator("..")).toContainText("jamais de valeurs brutes");
  await expect(q).not.toContainText("250"); // the corrupted value injected by the demo script is never shown
});

test("Quality: quarantine pagination (demo has only 8 rows: 25 are served through route interception)", async ({ page }) => {
  let real: { items: Record<string, unknown>[] } = { items: [] };
  let got = false;
  await page.route("**/api/quality/quarantine/**", async (route) => {
    if (!got) real = await (await route.fetch()).json();
    got = true; // keep a real first row as the template
    const u = new URL(route.request().url());
    const offset = Number(u.searchParams.get("offset") ?? 0);
    const limit = Number(u.searchParams.get("limit") ?? 10);
    const items = Array.from({ length: 25 }, (_, i) => ({ ...real.items[0], row: i }));
    await route.fulfill({ json: { ...real, total: 25, offset, limit, items: items.slice(offset, offset + limit) } });
  });
  await page.goto("/qualite");
  await page.getByLabel("Source").selectOption("hcp_population");
  const q = page.getByRole("region", { name: "Lignes en quarantaine" });
  const pager = page.getByRole("navigation", { name: "Pagination de la quarantaine" });
  await expect(pager).toContainText(/^1 à 10 sur 25/);
  await expect(q.getByRole("row")).toHaveCount(11);
  await pager.getByRole("button", { name: "Suivant" }).click();
  await expect(pager).toContainText(/^11 à 20 sur 25/);
  await pager.getByRole("button", { name: "Suivant" }).click();
  await expect(pager).toContainText(/^21 à 25 sur 25/);
  await expect(pager.getByRole("button", { name: "Suivant" })).toBeDisabled();
  await pager.getByRole("button", { name: "Précédent" }).click();
  await expect(pager).toContainText(/^11 à 20/);
});

test("Schemas: versions, diff sorted by severity, comparison change", async ({ page }) => {
  await page.goto("/schemas");
  await page.getByLabel("Source").selectOption("hcp_population");
  const versions = page.getByRole("region", { name: "Versions" });
  await expect(versions.getByRole("row").filter({ hasText: /^v\d/ }).first()).toBeVisible();
  expect(await versions.getByRole("row").count()).toBeGreaterThanOrEqual(3);
  const list = page.getByRole("list", { name: "Changements" });
  await expect(list).toBeVisible();
  const rank = ["Compatible", "À revoir", "Bloquant"];
  const badges = await list.locator(".badge").allInnerTexts();
  const ranks = badges.map((b) => rank.findIndex((r) => b.toLowerCase().includes(r.toLowerCase().slice(0, 4))));
  expect(ranks).toEqual([...ranks].sort((a, b) => b - a)); // most severe first
  const title = page.locator("#h-d");
  const before = await title.innerText();
  await page.getByLabel("De").selectOption({ index: 0 });
  await expect(title).not.toHaveText(before);
});

test("Review: unavailable via the API with CLI commands", async ({ page }) => {
  await page.goto("/revue");
  await expect(page.getByText("Indisponible via l'API")).toBeVisible();
  await expect(page.getByText("adp review list --status pending")).toBeVisible();
  await expect(page.getByText("adp review approve ID --by NOM")).toBeVisible();
});

test("theme toggle switches and persists (adp-theme)", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/");
  const btn = page.getByRole("button", { name: "Thème clair / sombre" });
  await expect(btn).toHaveAttribute("aria-pressed", "false");
  await btn.click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(btn).toHaveAttribute("aria-pressed", "true");
  expect(await page.evaluate(() => localStorage.getItem("adp-theme"))).toBe("dark");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.getByRole("button", { name: "Thème clair / sombre" })).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "Thème clair / sombre" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  expect(await page.evaluate(() => localStorage.getItem("adp-theme"))).toBe("light");
});

test("API error: accessible alert, then Retry recovers", async ({ page }) => {
  let fail = true;
  await page.route("**/api/runs*", (route) => (fail ? route.fulfill({ status: 401, contentType: "application/json", body: JSON.stringify({ detail: "invalid key" }) }) : route.continue()));
  await page.goto("/");
  const alert = page.getByRole("alert");
  await expect(alert).toBeVisible();
  const retry = alert.getByRole("button", { name: "Réessayer" });
  await expect(retry).toBeVisible();
  fail = false;
  await retry.click();
  await expect(page.getByRole("rowheader", { name: "hcp_population" })).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("API down (network abort): alert with Retry", async ({ page }) => {
  await page.route("**/api/runs*", (route) => route.abort("connectionrefused"));
  await page.goto("/");
  await expect(page.getByRole("alert").getByRole("button", { name: "Réessayer" })).toBeVisible();
});

test("reduced motion: content visible at once, no residual transform", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/");
  await expect(page.getByRole("rowheader", { name: "hcp_population" })).toBeVisible();
  const probe = () =>
    page.evaluate(() => {
      const sel = ["main", "main > div:last-child", ".card", ".kpi", ".empty"];
      return sel.flatMap((s) =>
        [...document.querySelectorAll<HTMLElement>(s)].map((e) => ({ s, t: getComputedStyle(e).transform, o: getComputedStyle(e).opacity, inline: e.style.transform, clip: e.style.clipPath })),
      );
    });
  for (const path of ["/", "/qualite", "/schemas", "/revue"]) {
    await page.goto(path);
    await expect(page.locator("main h1")).toBeVisible();
    const items = await probe(); // immediately, no waiting for an animation
    expect(items.length).toBeGreaterThan(0);
    for (const i of items) {
      expect(["none", "matrix(1, 0, 0, 1, 0, 0)"], `${path} ${i.s} transform`).toContain(i.t);
      expect(i.o, `${path} ${i.s} opacity`).toBe("1");
      expect(i.inline, `${path} ${i.s} inline transform`).not.toMatch(/translate/);
    }
    const fills = await page.evaluate(() => [...document.querySelectorAll<HTMLElement>(".fill")].map((e) => getComputedStyle(e).clipPath));
    for (const c of fills) expect(["none", "inset(0px)", "inset(0px 0% 0px 0px)"]).toContain(c);
  }
});

test("mobile 375px: no horizontal page scroll on any screen", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  for (const path of ["/", "/qualite", "/schemas", "/revue"]) {
    await page.goto(path);
    await expect(page.locator("main h1")).toBeVisible();
    await expect(page.locator("[aria-busy=true]")).toHaveCount(0, { timeout: 15_000 });
    const over = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(over, path).toBeLessThanOrEqual(0);
  }
});
