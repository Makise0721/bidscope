/**
 * Playwright globalSetup: validate the required ``BIDSCOPE_TEST_CONTROL_TOKEN``
 * before any spec runs.
 *
 * The database reset/migrate/seed runs in the ``webServer.command`` (see
 * ``playwright.config.ts``) rather than here, because Playwright starts the
 * webServer concurrently with globalSetup — provisioning the DB here would race
 * the server's lifespan (which expects the schema to exist). Running the DB
 * setup in the webServer command, chained with ``&&`` before ``api serve``,
 * guarantees the schema is ready first.
 *
 * globalSetup still runs before any project/test, so it is the right place to
 * fail fast on a missing token with a clear message instead of an opaque 401
 * mid-run. The caller is expected to set the token, e.g.
 * ``BIDSCOPE_TEST_CONTROL_TOKEN="e2e-$(date +%s)" npm run test:e2e``.
 */
export default async function globalSetup(): Promise<void> {
  if (!process.env.BIDSCOPE_TEST_CONTROL_TOKEN) {
    throw new Error(
      "[global-setup] BIDSCOPE_TEST_CONTROL_TOKEN is not set. Export it before running E2E, e.g.:\n" +
        '  BIDSCOPE_TEST_CONTROL_TOKEN="e2e-$(date +%s)" npm run test:e2e',
    );
  }
}
