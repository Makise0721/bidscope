import { Page, Response } from "@playwright/test";

const TEST_CONTROL_TOKEN = "test-controls-token";

/**
 * Generate a random test control token (used when the app is configured with
 * a non-default token). The default E2E server uses "test-controls-token".
 */
export function createTestToken(): string {
  return `test-token-${Math.random().toString(36).slice(2)}`;
}

/**
 * Call the test-only import-batch-2 endpoint to import the demo batch-2 bundle.
 * Only available when the server runs with BIDSCOPE_APP_MODE=test.
 */
export async function importBatch2(page: Page): Promise<Response> {
  return page.request.post("/api/test-controls/import-batch-2", {
    headers: { "X-Test-Control-Token": TEST_CONTROL_TOKEN },
  });
}

/**
 * Configure the test-only control to fail the next node execution.
 * The failure is one-shot: it applies to the very next run and is then cleared.
 */
export async function configureFailNextNode(
  page: Page,
  node: string,
): Promise<Response> {
  return page.request.post("/api/test-controls/fail-next-node", {
    headers: { "X-Test-Control-Token": TEST_CONTROL_TOKEN },
    data: { node },
  });
}

/**
 * Poll the run status until it reaches a terminal state (completed, failed, or
 * retryable) or the timeout elapses.
 */
export async function waitForRunCompletion(
  page: Page,
  runId: string,
  timeoutMs = 30_000,
): Promise<string> {
  const deadline = Date.now() + timeoutMs;
  let lastStatus = "";
  while (Date.now() < deadline) {
    const response = await page.request.get(`/api/runs/${runId}`);
    if (response.ok()) {
      const body = await response.json();
      lastStatus = body.status as string;
      if (
        lastStatus === "completed" ||
        lastStatus === "failed" ||
        lastStatus === "retryable"
      ) {
        return lastStatus;
      }
    }
    await page.waitForTimeout(500);
  }
  throw new Error(
    `Run ${runId} did not complete within ${timeoutMs}ms (last status: ${lastStatus})`,
  );
}
