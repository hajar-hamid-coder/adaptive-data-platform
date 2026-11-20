import { defineConfig } from "@playwright/test";

/** E2E against a real local demo stack (API + Vite) started by e2e/global-setup.ts on free ports. */
export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.e2e.ts",
  globalSetup: "./e2e/global-setup.ts",
  fullyParallel: true,
  forbidOnly: !!process.env["CI"],
  retries: 0,
  workers: 4,
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  outputDir: "test-results",
  use: { trace: "retain-on-failure", screenshot: "off", video: "off" },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
