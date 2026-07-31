import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { http, HttpResponse } from "msw";
import { App } from "../src/app/App";
import { RunReport } from "../src/features/workbench/RunReport";
import { getAdminToken } from "../src/auth/adminToken";
import { RUN_ID, server, REPRESENTATIVE_QUERY, reportWithEvidence, reportWithReviewClaims } from "../src/test/mockServer";

function renderApp(route = "/") {
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

describe("BidScope evidence workbench - main flow", () => {
  it("runs the representative query end-to-end", async () => {
    const user = userEvent.setup();
    renderApp();

    const input = await screen.findByLabelText(/enter your request/i);
    await user.type(input, REPRESENTATIVE_QUERY);
    await waitFor(() => expect(input).toHaveValue(REPRESENTATIVE_QUERY));

    const searchButton = await screen.findByRole("button", { name: /search/i });
    await user.click(searchButton);

    await waitFor(
      () => expect(screen.getByRole("heading", { name: "Confirm" })).toBeInTheDocument(),
      { timeout: 5000 },
    );
    expect(await screen.findByText(/智算中心/i)).toBeTruthy();
    expect(await screen.findByText(/四川/i)).toBeTruthy();

    const confirmRequest = vi.fn();
    server.use(
      http.post("/api/runs/:id/confirm", async ({ params, request }) => {
        confirmRequest({ id: params.id, method: request.method, token: request.headers.get("X-Admin-Token") });
        return HttpResponse.json({ id: RUN_ID, status: "completed" });
      }),
    );
    const approveButton = await screen.findByRole("button", { name: /approve/i });
    await user.click(approveButton);

    await waitFor(() =>
      expect(confirmRequest).toHaveBeenCalledWith({
        id: RUN_ID,
        method: "POST",
        token: "test-admin-token",
      }),
    );
    await waitFor(() => expect(screen.getByText(/report/i)).toBeInTheDocument());
  });

  it("clears an expired token and shows the auth-needed state", async () => {
    server.use(
      http.post("/api/runs", () =>
        HttpResponse.json({ detail: "invalid admin token" }, { status: 401 }),
      ),
    );
    const user = userEvent.setup();
    renderApp();

    const input = await screen.findByLabelText(/enter your request/i);
    await user.type(input, "四川服务器招标");
    await user.click(await screen.findByRole("button", { name: /search/i }));

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/authentication required/i));
    expect(getAdminToken()).toBeNull();
    expect(screen.getByText(/token required for api access/i)).toBeInTheDocument();
  });

  it("saves and clears the token for the current tab", async () => {
    const user = userEvent.setup();
    renderApp();

    const tokenInput = screen.getByLabelText("Admin token", { selector: "input" });
    await user.clear(tokenInput);
    await user.type(tokenInput, "replacement-token");
    await user.click(screen.getByRole("button", { name: /^save$/i }));
    expect(getAdminToken()).toBe("replacement-token");
    expect(screen.getByText(/token saved for this tab/i)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /clear admin token/i }));
    expect(getAdminToken()).toBeNull();
    expect(screen.getByText(/token required for api access/i)).toBeInTheDocument();
  });

  it("labels synthetic-demo records distinctly with plain-text URL", async () => {
    renderApp("/runs/11111111-1111-1111-1111-111111111111");

    await waitFor(() =>
      expect(screen.getByText(/合成演示数据/i)).toBeInTheDocument(),
    );
    const url = screen.getByText(/example\.invalid/);
    expect(url.tagName).toBe("SPAN");
    expect(url).not.toHaveAttribute("href");
  });

  it("uses the returned status instead of forcing confirmation", async () => {
    server.use(
      http.post("/api/runs", () =>
        HttpResponse.json({ id: "run-1", status: "completed", user_request: "四川服务器招标" }),
      ),
      http.get("/api/reports/run-1", () => HttpResponse.json(reportWithEvidence)),
    );

    const user = userEvent.setup();
    renderApp();

    const input = await screen.findByLabelText(/enter your request/i);
    await user.type(input, "四川服务器招标");
    await user.click(await screen.findByRole("button", { name: /search/i }));

    expect(await screen.findByRole("region", { name: "report" })).toBeVisible();
    expect(screen.queryByRole("region", { name: /confirm intent/i })).not.toBeInTheDocument();
  });

  it("renders citation evidence and keeps synthetic URLs as text", async () => {
    const user = userEvent.setup();
    render(<RunReport report={reportWithEvidence} />);

    await user.click(await screen.findByRole("button", { name: /open evidence/i }));
    const syntheticLabels = await screen.findAllByText(/合成演示数据/i);
    expect(syntheticLabels.length).toBeGreaterThanOrEqual(1);
    expect(syntheticLabels[0]).toBeVisible();
    const urls = screen.getAllByText("https://example.invalid/demo-001");
    expect(urls.length).toBeGreaterThanOrEqual(1);
    for (const url of urls) {
      expect(url.tagName).toBe("SPAN");
      expect(url).not.toHaveAttribute("href");
    }
    expect(screen.queryByRole("link", { name: /example\.invalid/ })).not.toBeInTheDocument();
    const labels = screen.getAllByText("预算金额证据");
    expect(labels.length).toBeGreaterThanOrEqual(1);
    expect(labels[0]).toBeVisible();
  });

  it("renders review claims with their doubt label and rationale", async () => {
    const user = userEvent.setup();
    render(<RunReport report={reportWithReviewClaims} />);

    await user.click(await screen.findByRole("button", { name: /open evidence/i }));
    // The UNSUPPORTED claim stays out of the verified claims section and is
    // rendered in the review queue with an explicit "证据冲突" label.
    const section = await screen.findByText(/复核队列/i);
    expect(section).toBeVisible();
    expect(await screen.findByText(/预算确定为 680 万元/)).toBeInTheDocument();
    expect(
      screen.getByTestId("review-status-unsupported"),
    ).toHaveTextContent("证据冲突");
    expect(
      screen.getByText(/证据记载金额为 500 万元，与 680 万元冲突/),
    ).toBeInTheDocument();
  });
});
