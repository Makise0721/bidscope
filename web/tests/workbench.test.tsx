import { describe, it, expect } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { App } from "../src/app/App";
import { server, REPRESENTATIVE_QUERY } from "../src/test/mockServer";

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

    // Enter the representative query.
    const input = await screen.findByLabelText(/enter your request/i);
    await user.type(input, REPRESENTATIVE_QUERY);
    await waitFor(() => expect(input).toHaveValue(REPRESENTATIVE_QUERY));

    // Submit creates a run.
    const searchButton = await screen.findByRole("button", { name: /search/i });
    await user.click(searchButton);

    // The parsed intent is shown as editable chips for confirmation.
    await waitFor(
      () => expect(screen.getByRole("heading", { name: "Confirm" })).toBeInTheDocument(),
      { timeout: 5000 },
    );
    expect(await screen.findByText(/智算中心/i)).toBeTruthy();
    expect(await screen.findByText(/四川/i)).toBeTruthy();

    // Approve the confirmation.
    const approveButton = await screen.findByRole("button", { name: /approve/i });
    await user.click(approveButton);

    // After completion the report is available.
    await waitFor(() => expect(screen.getByText(/report/i)).toBeInTheDocument());
  });

  it("labels synthetic-demo records distinctly with plain-text URL", async () => {
    renderApp("/runs/11111111-1111-1111-1111-111111111111");

    // Synthetic records carry a persistent label.
    await waitFor(() =>
      expect(screen.getByText(/合成演示数据/i)).toBeInTheDocument(),
    );
    // Synthetic records show their non-resolving URL as plain text.
    const url = screen.getByText(/example\.invalid/);
    expect(url.tagName).toBe("SPAN");
    expect(url).not.toHaveAttribute("href");
  });
});
