import { SCREENS, THEMES, expect, scan, test } from "./fixtures";

const VIEWPORTS = [
  { name: "desktop", size: { width: 1280, height: 800 } },
  { name: "mobile", size: { width: 375, height: 812 } },
] as const;

for (const vp of VIEWPORTS)
  for (const theme of THEMES)
    for (const s of SCREENS)
      test(`axe: ${s.name} / ${theme} / ${vp.name}: 0 serious or critical`, async ({ page }) => {
        await page.setViewportSize(vp.size);
        await page.addInitScript((t) => localStorage.setItem("adp-theme", t), theme);
        await page.goto(s.path);
        await expect(page.getByRole("heading", { level: 1, name: s.h1 })).toBeVisible();
        await expect(page.locator("[aria-busy=true]")).toHaveCount(0, { timeout: 15_000 }); // all queries settled
        await page.waitForTimeout(500); // entry animations settled (contrast is measured on final colours)
        const v = await scan(page);
        const blocking = v.filter((x) => x.impact === "serious" || x.impact === "critical");
        const minor = v.filter((x) => !blocking.includes(x));
        if (minor.length) console.log(`axe non-blocking ${s.name}/${theme}/${vp.name}: ${JSON.stringify(minor)}`);
        expect(blocking).toEqual([]);
      });
