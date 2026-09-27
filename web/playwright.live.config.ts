import { defineConfig, devices } from "@playwright/test";

/**
 * Real-API browser journeys (P4-07, P7-05, P7-19): no route mocks, nothing intercepted. The UI at
 * LIVE_BASE_URL (a `vite preview` or `vite dev` proxying /api to a running API, worker, ServiceNow mock
 * and database) is driven as the seeded users. Without LIVE_BASE_URL every journey is skipped.
 *
 *   LIVE_BASE_URL=http://127.0.0.1:5183 npm run test:e2e:live
 *
 * The journeys share one workspace, created by the first one, so they run in file order on one worker;
 * each run of the suite creates a fresh workspace. Screenshots land in e2e-live/screenshots/.
 */
const executablePath = process.env.PW_CHROMIUM_PATH || undefined;

export default defineConfig({
  testDir: "e2e-live",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: !!process.env.CI,
  timeout: 8 * 60_000,
  expect: { timeout: 20_000 },
  reporter: process.env.CI ? [["list"], ["html", { open: "never", outputFolder: "playwright-report-live" }]] : "list",
  outputDir: "test-results-live",
  use: {
    baseURL: process.env.LIVE_BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    actionTimeout: 30_000,
    launchOptions: executablePath ? { executablePath } : {},
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1400, height: 1000 } } }],
});
