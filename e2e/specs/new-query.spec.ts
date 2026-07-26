import { expect, test } from "@playwright/test";

/**
 * Flow: submit the representative unscheduled query and verify the report
 * renders inline on the workbench (no navigation, no confirmation).
 *
 * The demo graph auto-confirms high-confidence unscheduled queries
 * (``confirm_intent`` routes straight to retrieval), so the run reaches
 * ``completed`` and the Workbench renders ``RunReport`` in place. The app uses
 * ``BrowserRouter``, so we assert visible UI state rather than URL fragments.
 */
test.describe("New query flow", () => {
  test("creates a run and displays opportunities on the report page", async ({
    page,
  }) => {
    await page.goto("/");

    const input = page.getByLabel("Enter your request");
    await expect(input).toBeVisible();
    await input.fill("四川服务器招标");

    await page.getByRole("button", { name: "Search" }).click();

    // The Workbench renders the report inline once the run completes. There is
    // no navigation to /runs/:id for a fresh query on the workbench.
    const report = page.getByRole("region", { name: "report" });
    await expect(report).toBeVisible({ timeout: 30_000 });

    // The report lists at least one opportunity.
    const opportunities = report.locator(".opportunity-list .opportunity");
    await expect(opportunities.first()).toBeVisible();

    // Synthetic demo records are labelled and offer an evidence drawer trigger.
    const syntheticLabel = page.locator("[data-testid='synthetic-label']");
    await expect(syntheticLabel.first()).toBeVisible();
    await expect(syntheticLabel.first()).toContainText("合成演示数据");
    await expect(page.getByRole("button", { name: "Open evidence" }).first()).toBeVisible();
  });
});
