import { expect, test } from "@playwright/test";

/**
 * Flow: complete an unscheduled run, verify report items render with synthetic
 * provenance and that the synthetic source URL is plain text (not a link).
 *
 * The representative query auto-confirms, so no Approve click is needed. The
 * synthetic label and plain-url assertions are unconditional: every demo
 * notice is synthetic_demo and example.invalid URLs are never allow-listed, so
 * they always render as ``<span class="plain-url">``.
 */
test.describe("Report inspection", () => {
  test("displays report items with plain-text synthetic URLs", async ({
    page,
  }) => {
    await page.goto("/");

    const input = page.getByLabel("Enter your request");
    await input.fill("四川服务器招标");
    await page.getByRole("button", { name: "Search" }).click();

    // The run completes and the report renders inline.
    const report = page.getByRole("region", { name: "report" });
    await expect(report).toBeVisible({ timeout: 30_000 });

    // The report lists at least one opportunity.
    const items = report.locator(".opportunity-list .opportunity");
    await expect(items.first()).toBeVisible();

    // Synthetic records carry the synthetic label.
    const syntheticLabel = page.locator("[data-testid='synthetic-label']");
    await expect(syntheticLabel.first()).toBeVisible();
    await expect(syntheticLabel.first()).toContainText("合成演示数据");

    // Synthetic source URLs render as plain text (a <span>, not an <a>).
    const plainUrl = report.locator(".plain-url");
    await expect(plainUrl.first()).toBeVisible();
    await expect(plainUrl.first()).toContainText("example.invalid");

    // Each opportunity offers an evidence drawer trigger.
    await expect(page.getByRole("button", { name: "Open evidence" }).first()).toBeVisible();
  });
});
