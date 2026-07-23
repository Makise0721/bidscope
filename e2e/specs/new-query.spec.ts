import { test, expect } from "@playwright/test";

/**
 * Flow: Navigate to workbench, enter a query ("四川服务器招标"), submit,
 * wait for run to complete, verify results page shows opportunities.
 */
test.describe("New query flow", () => {
  test("creates a run and displays opportunities on the report page", async ({
    page,
  }) => {
    await page.goto("/");

    // The workbench is the landing page with a query input.
    const input = page.locator("#query-input");
    await expect(input).toBeVisible();
    await input.fill("四川服务器招标");

    // Submit the query.
    await page.getByRole("button", { name: "Search" }).click();

    // The run proceeds through the graph and completes. The Workbench
    // navigates to /runs/{runId} on completion.
    await page.waitForURL(/#\/runs\/[0-9a-f-]+$/, { timeout: 30_000 });

    // The report page renders the list of opportunities.
    await expect(page.getByRole("region", { name: "report" })).toBeVisible();
    const opportunities = page.locator(".opportunity-list .opportunity");
    await expect(opportunities.first()).toBeVisible();
  });
});
