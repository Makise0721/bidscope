import { test, expect } from "@playwright/test";

/**
 * Flow: Create a low-confidence or scheduled query that triggers confirmation,
 * verify the "awaiting confirmation" UI appears, approve the confirmation,
 * verify the run proceeds.
 *
 * The Workbench always sets phase to "awaiting_confirmation" immediately after
 * createRun succeeds (the fake model produces a structured intent that then
 * routes to the confirmation UI). The IntentConfirmation component shows an
 * Approve button; clicking it calls confirmRun and drives the run forward.
 */
test.describe("Intent confirmation flow", () => {
  test("awaits confirmation then proceeds after approve", async ({ page }) => {
    await page.goto("/");

    const input = page.locator("#query-input");
    await input.fill("四川服务器招标");
    await page.getByRole("button", { name: "Search" }).click();

    // The confirmation panel should appear with an Approve button.
    const confirmation = page.getByRole("region", { name: "confirm intent" });
    await expect(confirmation).toBeVisible({ timeout: 15_000 });
    await expect(confirmation).toContainText("Confirm");

    // Approve the confirmation.
    await confirmation.getByRole("button", { name: "Approve" }).click();

    // The run proceeds to completion and navigates to the report page.
    await page.waitForURL(/#\/runs\/[0-9a-f-]+$/, { timeout: 30_000 });
    await expect(page.getByRole("region", { name: "report" })).toBeVisible();
  });
});
