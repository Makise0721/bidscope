import { test, expect } from "@playwright/test";
import { configureFailNextNode } from "../fixtures/test-helper";

/**
 * Flow: Configure the test-only control to fail the next node, submit a query,
 * verify the error/failure UI appears, retry the failed node, verify the run
 * completes.
 *
 * When ``fail-next-node`` is armed, the background graph executor short-circuits
 * with a ``retryable`` status before the first node runs. The confirmation step
 * then fails (because the run is not in ``awaiting_confirmation``), which the
 * Workbench surfaces as a "Failed" status. The operator retries the run from the
 * run-history page; the second run proceeds normally because the one-shot flag
 * has been cleared.
 */
test.describe("Retry on injected node failure", () => {
  test("fails the next node then recovers via retry", async ({ page }) => {
    await page.goto("/");

    // Arm the one-shot failure for parse_intent.
    const failResponse = await configureFailNextNode(page, "parse_intent");
    expect(failResponse.ok()).toBeTruthy();

    // Submit a query.
    const input = page.locator("#query-input");
    await input.fill("四川服务器招标");
    await page.getByRole("button", { name: "Search" }).click();

    // The confirmation panel appears (Workbench always shows it first).
    const confirmation = page.getByRole("region", { name: "confirm intent" });
    await expect(confirmation).toBeVisible({ timeout: 15_000 });

    // Approve — the run is in retryable (not awaiting_confirmation), so the
    // confirm API rejects it and the Workbench surfaces a "Failed" badge.
    await confirmation.getByRole("button", { name: "Approve" }).click();
    await expect(page.locator(".status-status-failed")).toBeVisible({
      timeout: 15_000,
    });

    // Navigate to run history.
    await page.goto("/runs");
    await expect(
      page.getByRole("heading", { name: "Run history" }),
    ).toBeVisible();

    // The run should appear as retryable with an enabled Retry button.
    const retryButton = page.getByRole("button", { name: /Retry/ }).first();
    await expect(retryButton).toBeVisible();
    await expect(retryButton).toBeEnabled();
    await retryButton.click();

    // The run status transitions from retryable to completed. Poll the
    // runs list until no retryable run remains.
    await expect
      .poll(
        async () => {
          const response = await page.request.get("/api/runs?status=retryable");
          const body = await response.json();
          return (body.items ?? []).length;
        },
        { timeout: 30_000 },
      )
      .toBe(0);
  });
});
