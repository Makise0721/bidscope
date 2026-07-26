import { expect, test } from "@playwright/test";

/**
 * Flow: submit a scheduled query (which routes to confirmation), verify the
 * "awaiting confirmation" UI appears, approve it, verify the run completes.
 *
 * The fake intent parser produces a ``schedule`` only when the request
 * contains ``每周``/``每月``; a scheduled run routes through ``confirm_intent``
 * to the ``pause`` interrupt, so the run pauses at ``awaiting_confirmation``.
 * The Workbench renders ``IntentConfirmation`` only while the run is in that
 * state. Approval resumes the run through to completion.
 *
 * Uses visible UI state (no hash-URL assertions — the app uses BrowserRouter).
 */
test.describe("Intent confirmation flow", () => {
  test("awaits confirmation then proceeds after approve", async ({ page }) => {
    await page.goto("/");

    const input = page.getByLabel("Enter your request");
    await input.fill("四川重庆智算中心服务器招标 每周一9点");
    await page.getByRole("button", { name: "Search" }).click();

    // The confirmation panel appears with an Approve button.
    const confirmation = page.getByRole("region", { name: "confirm intent" });
    await expect(confirmation).toBeVisible({ timeout: 15_000 });
    await expect(confirmation).toContainText("Confirm");

    // Approve the confirmation.
    await confirmation.getByRole("button", { name: "Approve" }).click();

    // The run resumes through retrieval and delivery, rendering the report.
    await expect(page.getByRole("region", { name: "report" })).toBeVisible({
      timeout: 30_000,
    });
  });
});
