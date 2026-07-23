import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./specs",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: [["list"], ["html", { open: "never" }]],
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
    command: "npm run build:web && uv run --offline bidscope api --port 8001",
    url: "http://localhost:8001/healthz",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: {
      BIDSCOPE_APP_MODE: "test",
      BIDSCOPE_DATABASE_URL: "postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_e2e",
      BIDSCOPE_CHECKPOINT_DATABASE_URL: "postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_e2e",
    },
  },
});
