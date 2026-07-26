import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { http, HttpResponse } from "msw";
import { App } from "../src/app/App";
import { RunReport } from "../src/features/workbench/RunReport";
import { RUN_ID, server, REPRESENTATIVE_QUERY, reportWithEvidence } from "../src/test/mockServer";

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

/**
 * Minimal in-test EventSource stub. jsdom does not implement EventSource,
 * and the production SSE consumer only needs `addEventListener`, `onmessage`,
 * `close`, and the standard EventTarget dispatch surface for the `terminal`
 * event. We expose `__urls`, `__emit`, and `__emitTerminal` on each instance
 * so individual tests can drive events synchronously.
 */
interface FakeEventSource {
  url: string;
  readyState: number;
  onerror: ((event: Event) => void) | null;
  onmessage: ((event: MessageEvent) => void) | null;
  onopen: ((event: Event) => void) | null;
  addEventListener: (type: string, listener: (event: MessageEvent) => void) => void;
  removeEventListener: (type: string, listener: (event: MessageEvent) => void) => void;
  close: () => void;
  __emit: (event: { event?: string; data: string; id?: string }) => void;
  __emitTerminal: (status: string) => void;
}

type EventSourceConstructor = new (url: string) => FakeEventSource;

const fakeEventSourceInstances: FakeEventSource[] = [];

function installFakeEventSource(): void {
  const FakeES: EventSourceConstructor = function (this: FakeEventSource, url: string) {
    this.url = url;
    this.readyState = 0;
    this.onerror = null;
    this.onmessage = null;
    this.onopen = null;
    const listeners = new Map<string, Set<(event: MessageEvent) => void>>();
    this.addEventListener = (type, listener) => {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type)!.add(listener);
    };
    this.removeEventListener = (type, listener) => {
      listeners.get(type)?.delete(listener);
    };
    const dispatch = (type: string, data: string, id?: string) => {
      const messageEvent = new MessageEvent(type, { data, lastEventId: id ?? "" });
      // Type-specific listeners.
      listeners.get(type)?.forEach((listener) => listener(messageEvent));
      // onmessage fires only for frames without an explicit event field.
      if (type === "message" && this.onmessage) {
        this.onmessage(messageEvent);
      }
    };
    this.__emit = ({ event: type = "message", data, id }) => dispatch(type, data, id);
    this.__emitTerminal = (status: string) => {
      dispatch("terminal", JSON.stringify({ status, terminal: true }), "terminal");
    };
    this.close = () => {
      this.readyState = 2;
    };
    fakeEventSourceInstances.push(this);
    return this;
  } as unknown as EventSourceConstructor;
  // Install on global + window so production code reading either sees it.
  (globalThis as { EventSource?: EventSourceConstructor }).EventSource = FakeES;
  (window as { EventSource?: EventSourceConstructor }).EventSource = FakeES;
}

function restoreEventSource(saved: EventSourceConstructor | undefined): void {
  if (saved === undefined) {
    delete (globalThis as { EventSource?: EventSourceConstructor }).EventSource;
    delete (window as { EventSource?: EventSourceConstructor }).EventSource;
  } else {
    (globalThis as { EventSource?: EventSourceConstructor }).EventSource = saved;
    (window as { EventSource?: EventSourceConstructor }).EventSource = saved;
  }
}

describe("BidScope evidence workbench - main flow", () => {
  let savedEventSource: EventSourceConstructor | undefined;

  beforeEach(() => {
    fakeEventSourceInstances.length = 0;
    savedEventSource = (globalThis as { EventSource?: EventSourceConstructor }).EventSource;
    installFakeEventSource();
  });

  afterEach(() => {
    restoreEventSource(savedEventSource);
    fakeEventSourceInstances.length = 0;
  });

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

    // Approve the confirmation and verify the runtime confirmation request.
    const confirmRequest = vi.fn();
    server.use(
      http.post("/api/runs/:id/confirm", async ({ params, request }) => {
        confirmRequest({ id: params.id, method: request.method });
        return HttpResponse.json({ id: RUN_ID, status: "completed" });
      }),
    );
    const approveButton = await screen.findByRole("button", { name: /approve/i });
    await user.click(approveButton);

    await waitFor(() =>
      expect(confirmRequest).toHaveBeenCalledWith({ id: RUN_ID, method: "POST" }),
    );

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

  it("uses the returned status instead of forcing confirmation", async () => {
    // A run that returns immediately-completed should skip the confirmation
    // region and surface the report directly.
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
    // Synthetic capture_kind renders the persistent label (inline badge and in
    // the drawer); at least one instance must be visible.
    const syntheticLabels = await screen.findAllByText(/合成演示数据/i);
    expect(syntheticLabels.length).toBeGreaterThanOrEqual(1);
    expect(syntheticLabels[0]).toBeVisible();
    // example.invalid URL stays a text node, never an anchor. It may appear
    // both inline (opportunity list) and inside the drawer's provenance block;
    // every match must be a SPAN without href.
    const urls = screen.getAllByText("https://example.invalid/demo-001");
    expect(urls.length).toBeGreaterThanOrEqual(1);
    for (const url of urls) {
      expect(url.tagName).toBe("SPAN");
      expect(url).not.toHaveAttribute("href");
    }
    expect(screen.queryByRole("link", { name: /example\.invalid/ })).not.toBeInTheDocument();
    // Citation label from the report DTO is rendered. The same label may
    // surface both in the citation list and in the claim's citation reference
    // list, so accept any positive count.
    const labels = screen.getAllByText("预算金额证据");
    expect(labels.length).toBeGreaterThanOrEqual(1);
    expect(labels[0]).toBeVisible();
  });
});
