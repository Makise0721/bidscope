import { defineConfig, devices } from "@playwright/test";

/**
 * E2E configuration for BidScope.
 *
 * The webServer boots the real API (`bidscope api serve`) against the
 * dedicated `bidscope_e2e` database, and `globalSetup` migrates + seeds that
 * database before any spec runs. The SPA is built first so uvicorn can serve
 * the static bundle from `backend/.../static`.
 *
 * The caller must export `BIDSCOPE_TEST_CONTROL_TOKEN` (e.g.
 * `BIDSCOPE_TEST_CONTROL_TOKEN="e2e-$(date +%s)" npm run test:e2e`); globalSetup
 * throws early with a clear message if it is missing. Test mode disables the
 * admin-token guard on `/api/*`, so no `BIDSCOPE_ADMIN_TOKEN` is required.
 */
export default defineConfig({
  testDir: "./specs",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: [["list"], ["html", { open: "never" }]],
  globalSetup: "./global-setup",
  use: {
    baseURL: process.env.E2E_BASE_URL || "http://localhost:8001",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [
    {
      name: "desktop",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } },
    },
    {
      name: "mobile",
      use: { ...devices["Pixel 5"], viewport: { width: 390, height: 844 } },
    },
  ],
  webServer: {
    // Build the SPA, then reset+migrate+seed the e2e database, THEN start the
    // API. Chaining with ``&&`` guarantees the schema and seed data are ready
    // before the server's lifespan runs (globalSetup runs concurrently with
    // the webServer, so DB provisioning cannot live there without racing the
    // server startup).
    command:
      "npm run build:web && npm run e2e:db-setup && uv run --offline bidscope api serve --host 127.0.0.1 --port 8001",
    url: "http://localhost:8001/healthz",
    reuseExistingServer: !process.env.CI,
    timeout: 180_000,
    env: {
      BIDSCOPE_APP_MODE: "test",
      BIDSCOPE_DATABASE_URL: "postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_e2e",
      BIDSCOPE_CHECKPOINT_DATABASE_URL: "postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_e2e",
      BIDSCOPE_TEST_CONTROL_TOKEN: process.env.BIDSCOPE_TEST_CONTROL_TOKEN ?? "",
    },
  },
});
