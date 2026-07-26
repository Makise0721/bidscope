import { expect, test } from "@playwright/test";
import { importBatch2, runSchedulerTick } from "../fixtures/test-helper";

/**
 * Subscription + batch-2 import flow.
 *
 * Subscriptions are derived from a *completed, confirmed run whose intent
 * carries a schedule*: the API takes ``{ run_id }`` and materializes the
 * subscription from that run's normalized search intent (the cron expression
 * and timezone come from the run's schedule, never from the request body).
 *
 * To surface inbox events we:
 *   1. Drive the UI to create a completed run from a scheduled query and
 *      capture its run id from the ``POST /api/runs`` response.
 *   2. POST ``{ run_id }`` to ``/api/subscriptions`` and expect 201.
 *   3. Run one scheduler tick against batch-1 (already seeded) so the
 *      subscription diffs the notices for the first time and emits
 *      ``new_notice`` events while populating its seen-item cursor.
 *   4. Import batch-2 (changed budgets/deadlines) and run a second tick; the
 *      content-hash diff now emits ``material_change`` events.
 *   5. Assert the inbox lists both event kinds via their message prefixes
 *      (``New notice:`` / ``Material change in:``), non-conditional.
 *
 * Scheduler ticks are driven via the test-only ``run-scheduler-tick`` endpoint
 * (registered only in test mode), which runs the real subscription pipeline
 * with a far-future ``now`` so every active subscription is immediately due —
 * no waiting for the next cron occurrence and no separate scheduler process.
 */
test.describe("Subscription + batch-2 import", () => {
  test("creates a subscription and surfaces new + material-change inbox events", async ({
    page,
  }) => {
    // 1. Create a completed run from a scheduled query and capture its id.
    await page.goto("/");
    const input = page.getByLabel("Enter your request");
    await input.fill("四川重庆智算中心服务器招标 每周一9点");

    const createRunResponse = page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/runs") && response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Search" }).click();
    const createRunRequest = await (await createRunResponse).json();
    const runId: string = createRunRequest.id;
    expect(runId).toBeTruthy();

    // The scheduled query pauses at awaiting_confirmation; approve it so the
    // run proceeds to completion (required before a subscription can be made).
    const confirmation = page.getByRole("region", { name: "confirm intent" });
    await expect(confirmation).toBeVisible({ timeout: 15_000 });
    await confirmation.getByRole("button", { name: "Approve" }).click();
    await expect(page.getByRole("region", { name: "report" })).toBeVisible({
      timeout: 30_000,
    });

    // 2. Create the subscription from the completed run.
    const createSubscriptionResponse = await page.request.post(
      "/api/subscriptions",
      { data: { run_id: runId } },
    );
    expect(createSubscriptionResponse.status()).toBe(201);
    const subscription = await createSubscriptionResponse.json();
    expect(subscription.id).toBeTruthy();

    // 3. First scheduler tick: diff notices for the first time → new_notice.
    //    The test-control endpoint drives the real subscription pipeline with a
    //    far-future ``now`` so the just-created subscription is immediately due.
    const tick1 = await runSchedulerTick(page);
    expect(tick1.ok()).toBeTruthy();
    const tick1Body = await tick1.json();
    expect(tick1Body.ran).toBeGreaterThanOrEqual(1);

    // 4. Import batch-2 (changed budgets/deadlines), then tick again so the
    //    content-hash diff surfaces material_change events.
    const importResponse = await importBatch2(page);
    expect(importResponse.ok()).toBeTruthy();
    const importBody = await importResponse.json();
    expect(importBody.import_batch_2).toBe("ok");
    expect(importBody.status).toBe("success");
    const tick2 = await runSchedulerTick(page);
    expect(tick2.ok()).toBeTruthy();

    // 5. The subscriptions page renders both the schedule and the inbox.
    await page.goto("/subscriptions");
    await expect(page.getByRole("heading", { name: "Subscriptions" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Inbox" })).toBeVisible();

    // The inbox lists both new-notice and material-change events. The event
    // types are rendered via their human-readable message prefixes
    // ("New notice:" / "Material change in:"), which the subscription service
    // sets when it emits each InboxEvent. Both kinds are produced across the
    // two ticks (new_notice on the first diff, material_change after batch-2's
    // budget/deadline edits), so both must be visible (non-conditional).
    const inboxList = page.locator(".inbox-list");
    await expect(inboxList).toBeVisible();
    await expect(inboxList.locator(".inbox-item").first()).toBeVisible();
    await expect(inboxList).toContainText("New notice:");
    await expect(inboxList).toContainText("Material change in:");
  });
});
