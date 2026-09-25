import { defineConfig, devices } from "@playwright/test";

/**
 * Journey and accessibility tests against the production bundle (`vite preview`), with the API
 * mocked by route interception (e2e/fixtures.ts). Run `npm run build` first.
 *
 * Browsers: CI installs Chromium with `npx playwright install --with-deps chromium`. Elsewhere a
 * preinstalled browser is used via PLAYWRIGHT_BROWSERS_PATH, or PW_CHROMIUM_PATH points at a
 * Chromium binary when the installed build does not match this Playwright version.
 */
const PORT = Number(process.env.PW_PORT ?? 4173);
const executablePath = process.env.PW_CHROMIUM_PATH || undefined;

export default defineConfig({
  testDir: "e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]] : "list",
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "retain-on-failure",
    launchOptions: executablePath ? { executablePath } : {},
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `npx vite preview --host 127.0.0.1 --port ${PORT} --strictPort`,
    url: `http://127.0.0.1:${PORT}/login`,
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
});
