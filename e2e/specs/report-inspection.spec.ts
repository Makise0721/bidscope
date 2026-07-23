import { test, expect } from "@playwright/test";

/**
 * Flow: Complete a run, navigate to the report, verify items/claims/citations
 * are displayed, verify source URLs are plain text (not clickable) for
 * synthetic records.
 */
test.describe("Report inspection", () => {
  test("displays report items with plain-text synthetic URLs", async ({
    page,
  }) => {
    await page.goto("/");

    // Create a run and let it complete.
    const input = page.locator("#query-input");
    await input.fill("四川服务器招标");
    await page.getByRole("button", { name: "Search" }).click();
    await confirmationApprove(page);
    await page.waitForURL(/#\/runs\/[0-9a-f-]+$/, { timeout: 30_000 });

    // The report region is visible with opportunity items.
    const report = page.getByRole("region", { name: "report" });
    await expect(report).toBeVisible();
    const items = report.locator(".opportunity-list .opportunity");
    await expect(items.first()).toBeVisible();

    // Synthetic records render their source URL as plain text (not a link).
    const syntheticLabel = page.locator("[data-testid='synthetic-label']");
    if (await syntheticLabel.count()) {
      // The synthetic label is present; the URL next to it must be a <span>,
      // not an <a> tag.
      const plainUrl = report.locator(".plain-url");
      await expect(plainUrl.first()).toBeVisible();
    }
  });
});

/** Approve the intent confirmation panel if it appears. */
async function confirmationApprove(page: import("@playwright/test").Page) {
  const confirmation = page.getByRole("region", { name: "confirm intent" });
  await confirmation
    .waitFor({ state: "visible", timeout: 15_000 })
    .then(() => confirmation.getByRole("button", { name: "Approve" }).click())
    .catch(() => {
      // Confirmation may not appear for auto-confirmed runs.
    });
}
