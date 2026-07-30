import { describe, expect, it } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { http, HttpResponse } from "msw";
import { App } from "../src/app/App";
import { RUN_ID, server } from "../src/test/mockServer";

function renderApp(route: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[route]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("BidScope operational views", () => {
  it("filters run history and enables retry only for retryable runs", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/api/runs", () =>
        HttpResponse.json({
          items: [
            {
              id: "run-retryable",
              status: "retryable",
              user_request: "四川服务器招标",
              retryable: true,
            },
            {
              id: "run-completed",
              status: "completed",
              user_request: "重庆数据中心招标",
              retryable: false,
            },
          ],
        }),
      ),
      http.post("/api/runs/run-retryable/retry", () =>
        HttpResponse.json({ id: "run-retryable", status: "pending" }),
      ),
    );

    renderApp("/runs");

    expect(await screen.findByRole("heading", { name: /run history/i })).toBeInTheDocument();
    expect(screen.getByText("四川服务器招标")).toBeInTheDocument();
    expect(screen.getByText("重庆数据中心招标")).toBeInTheDocument();

    const retryButtons = screen.getAllByRole("button", { name: /retry/i });
    expect(retryButtons.some((button) => !(button as HTMLButtonElement).disabled)).toBe(true);
    expect(retryButtons.some((button) => (button as HTMLButtonElement).disabled)).toBe(true);

    const statusFilter = screen.getByRole("combobox", { name: /status/i });
    await user.selectOptions(statusFilter, "retryable");

    expect(screen.getByText("四川服务器招标")).toBeInTheDocument();
    expect(screen.queryByText("重庆数据中心招标")).not.toBeInTheDocument();
  });

  it("pauses and resumes subscriptions while showing inbox read states", async () => {
    const user = userEvent.setup();
    let status = "active";
    server.use(
      http.get("/api/subscriptions", () =>
        HttpResponse.json([
          {
            id: "subscription-1",
            status,
            cron_expression: "0 9 * * 1",
            next_run_at: "2026-07-27T01:00:00+00:00",
          },
        ]),
      ),
      http.post("/api/subscriptions/subscription-1/pause", () => {
        status = "paused";
        return HttpResponse.json({ id: "subscription-1", status });
      }),
      http.post("/api/subscriptions/subscription-1/resume", () => {
        status = "active";
        return HttpResponse.json({ id: "subscription-1", status });
      }),
      http.get("/api/inbox-events", () =>
        HttpResponse.json({
          items: [
            {
              id: "inbox-new",
              event_type: "new_notice",
              title: "New server tender",
              read: false,
            },
            {
              id: "inbox-change",
              event_type: "material_change",
              title: "Deadline changed",
              read: true,
            },
          ],
        }),
      ),
    );

    renderApp("/subscriptions");

    expect(await screen.findByText(/active/i)).toBeInTheDocument();
    expect(screen.getByText("New server tender")).toBeInTheDocument();
    expect(screen.getByText("Deadline changed")).toBeInTheDocument();
    expect(screen.getByText("Unread", { exact: true })).toBeInTheDocument();
    expect(screen.getByText("Read", { exact: true })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /pause/i }));
    await waitFor(() => expect(screen.getByText(/paused/i)).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /resume/i }));
    await waitFor(() => expect(screen.getByText(/active/i)).toBeInTheDocument());
  });

  it("shows source provenance with stale and invalid snapshot warnings", async () => {
    server.use(
      http.get("/api/sources", () =>
        HttpResponse.json({
          items: [
            {
              source: "ccgp",
              status: "valid",
              latest_valid_bundle: {
                bundle_id: "ccgp-central-20260718",
                capture_kind: "curated_public_excerpt",
                retrieved_at: "2026-07-18T00:00:00+00:00",
                hash_prefix: "abc12345",
                parser_version: "ccgp-v1",
                source_urls: ["https://:@www.ccgp.gov.cn/path"],
              },
              validation_warnings: [],
            },
            {
              source: "ggzy",
              status: "stale",
              latest_valid_bundle: null,
              validation_warnings: ["snapshot_stale"],
            },
            {
              source: "synthetic_demo",
              status: "invalid",
              latest_valid_bundle: {
                bundle_id: "synthetic-demo-20260718",
                capture_kind: "synthetic_fixture",
                retrieved_at: "2026-07-18T00:00:00+00:00",
                hash_prefix: "demo1234",
                parser_version: "synthetic-demo-v1",
                source_urls: ["https://example.invalid/demo-001"],
              },
              validation_warnings: ["snapshot_integrity_error"],
            },
          ],
        }),
      ),
    );

    renderApp("/sources");

    expect(await screen.findByRole("heading", { name: /data sources/i })).toBeInTheDocument();
    expect(screen.getByText(/curated public excerpt/i)).toBeInTheDocument();
    expect(screen.getByText("ccgp-v1")).toBeInTheDocument();
    expect(screen.getByText("abc12345")).toBeInTheDocument();
    expect(screen.getByText("Stale", { exact: true })).toBeInTheDocument();
    expect(screen.getByText(/snapshot_stale/i)).toBeInTheDocument();
    expect(screen.getByText("invalid", { exact: true })).toBeInTheDocument();
    expect(screen.getByText(/snapshot_integrity_error/i)).toBeInTheDocument();

    const syntheticUrl = screen.getByText("https://example.invalid/demo-001");
    expect(syntheticUrl.tagName).toBe("SPAN");
    expect(syntheticUrl).not.toHaveAttribute("href");

    const credentialUrl = screen.getByText("https://:@www.ccgp.gov.cn/path");
    expect(credentialUrl.tagName).toBe("SPAN");
    expect(credentialUrl).not.toHaveAttribute("href");
  });

  it.each([
    ["synthetic URL", "https://example.invalid/demo-001"],
    ["official lookalike URL", "https://www.ccgp.gov.cn.evil.test/foo"],
    ["credential-bearing URL", "https://user:secret@www.ccgp.gov.cn/path"],
    ["empty-userinfo URL", "https://@www.ccgp.gov.cn/path"],
    ["empty-password URL", "https://:@www.ccgp.gov.cn/path"],
  ])("renders a %s as plain text for ccgp report items", async (_label, url) => {
    server.use(
      http.get("/api/reports/:id", () =>
        HttpResponse.json({
          id: RUN_ID,
          run_id: RUN_ID,
          conditions: {},
          items: [
            {
              title: "CCGP report item",
              source: "ccgp",
              url,
            },
          ],
        }),
      ),
    );

    renderApp(`/runs/${RUN_ID}`);

    const reportUrl = await screen.findByText(url);
    expect(reportUrl.tagName).toBe("SPAN");
    expect(reportUrl).not.toHaveAttribute("href");
  });

  it("renders evaluation cards with measured values separate from targets", async () => {
    server.use(
      http.get("/api/evaluations", () =>
        HttpResponse.json({
          items: [
            {
              id: "eval-run-1",
              dataset_version: "retrieval-v1",
              model: "deterministic-fake",
              environment: "test",
              pricing_snapshot_date: "2026-07-18",
              metrics: {
                "Recall@10": { measured: 0.82, target: 0.8 },
                citation_coverage: { measured: 0.97, target: 0.95 },
              },
            },
          ],
        }),
      ),
    );

    renderApp("/evaluation");

    expect(await screen.findByRole("heading", { name: /evaluation/i })).toBeInTheDocument();
    expect(screen.getByText("retrieval-v1")).toBeInTheDocument();
    expect(screen.getByText("deterministic-fake")).toBeInTheDocument();
    expect(screen.getAllByText(/measured/i).length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText(/target/i).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("0.82")).toBeInTheDocument();
    expect(screen.getByText("0.80")).toBeInTheDocument();
    expect(screen.getByText("0.97")).toBeInTheDocument();
    expect(screen.getByText("0.95")).toBeInTheDocument();
  });
});
