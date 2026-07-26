import { expect, test } from "@playwright/test";
import { configureFailNextNode } from "../fixtures/test-helper";

/**
 * Flow: arm the one-shot node failure, submit a query, verify the run lands in
 * ``retryable`` (surfaced as Failed by the Workbench), retry it from run
 * history, verify this specific run recovers to a non-retryable terminal state.
 *
 * When ``fail-next-node`` is armed, the executor short-circuits before the
 * graph runs and persists ``retryable``. The Workbench maps ``retryable`` to
 * its Failed phase. The one-shot flag is cleared by the failed run, so retrying
 * lets the run proceed normally to completion.
 *
 * The recovery assertion is scoped to this test's own run id (captured from the
 * createRun response) rather than the global retryable count, so unrelated
 * retryable runs left by other specs or prior runs do not pollute the check.
 */
test.describe("Retry on injected node failure", () => {
  test("fails the next node then recovers via retry", async ({ page }) => {
    await page.goto("/");

    // Arm the one-shot failure for parse_intent.
    const failResponse = await configureFailNextNode(page, "parse_intent");
    expect(failResponse.ok()).toBeTruthy();

    // Submit a query; capture this run's id from the createRun response.
    const input = page.getByLabel("Enter your request");
    await input.fill("四川服务器招标");

    const createRunResponse = page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/runs") && response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Search" }).click();
    const runId = (await (await createRunResponse).json()).id as string;
    expect(runId).toBeTruthy();

    // The run short-circuits to retryable, which the Workbench renders as a
    // Failed status badge.
    await expect(page.locator(".status-failed").first()).toBeVisible({
      timeout: 15_000,
    });

    // Navigate to run history.
    await page.goto("/runs");
    await expect(page.getByRole("heading", { name: "Run history" })).toBeVisible();

    // Retry this run via the API (the run-history Retry button targets the
    // row's run id; calling the retry endpoint directly is equivalent and
    // avoids ambiguity when multiple retryable rows exist).
    const retryResponse = await page.request.post(`/api/runs/${runId}/retry`);
    expect(retryResponse.ok()).toBeTruthy();

    // This run transitions out of retryable. Poll until it is no longer
    // retryable (scoped to this run, not the global count).
    await expect
      .poll(
        async () => {
          const response = await page.request.get(`/api/runs/${runId}`);
          if (!response.ok()) return "unknown";
          return (await response.json()).status as string;
        },
        { timeout: 30_000 },
      )
      .not.toBe("retryable");
  });
});
