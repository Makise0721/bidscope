import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ADMIN_TOKEN_STORAGE_KEY,
  UnauthorizedError,
  buildAuthHeaders,
  clearAdminToken,
  getAdminToken,
  onUnauthorized,
  setAdminToken,
} from "../auth/adminToken";
import { downloadDocx, requestJson, streamRunEvents } from "../api/client";

describe("admin token authentication", () => {
  beforeEach(() => {
    sessionStorage.clear();
    localStorage.clear();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("stores a trimmed token in sessionStorage only", () => {
    const sessionSetItem = vi.spyOn(Storage.prototype, "setItem");

    setAdminToken("  tab-secret  ");

    expect(sessionStorage.getItem(ADMIN_TOKEN_STORAGE_KEY)).toBe("tab-secret");
    expect(localStorage.getItem(ADMIN_TOKEN_STORAGE_KEY)).toBeNull();
    expect(sessionSetItem).toHaveBeenCalledWith(ADMIN_TOKEN_STORAGE_KEY, "tab-secret");
    expect(getAdminToken()).toBe("tab-secret");
  });

  it("rejects an empty token without replacing the current token", () => {
    setAdminToken("existing-secret");

    expect(setAdminToken("  ")).toBe(false);
    expect(getAdminToken()).toBe("existing-secret");

    clearAdminToken();
    expect(getAdminToken()).toBeNull();
    expect(localStorage.length).toBe(0);
  });

  it("builds auth headers without exposing a missing token", () => {
    clearAdminToken();
    const withoutToken = buildAuthHeaders({ Accept: "application/json" });
    expect(withoutToken.get("Accept")).toBe("application/json");
    expect(withoutToken.has("X-Admin-Token")).toBe(false);

    setAdminToken("tab-secret");
    const withToken = buildAuthHeaders({ "Content-Type": "application/json" });
    expect(withToken.get("Content-Type")).toBe("application/json");
    expect(withToken.get("X-Admin-Token")).toBe("tab-secret");
  });

  it("clears the token, emits unauthorized, and throws a stable error on 401", async () => {
    setAdminToken("expired-secret");
    const unauthorized = vi.fn();
    const unsubscribe = onUnauthorized(unauthorized);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(null, { status: 401, statusText: "Unauthorized" }),
    );

    await expect(requestJson("/api/protected")).rejects.toBeInstanceOf(UnauthorizedError);

    expect(getAdminToken()).toBeNull();
    expect(unauthorized).toHaveBeenCalledTimes(1);
    expect(unauthorized.mock.calls[0]).toEqual([]);
    unsubscribe();
  });

  it("parses chunked CRLF SSE frames, multiline data, typed events, and terminal", async () => {
    setAdminToken("tab-secret");
    const encoder = new TextEncoder();
    const chunks = [
      "event: intent_parsed\r\nid: 0\r\ndata: {\"seq\":0,\"node\":\"parse\",",
      "\r\ndata: \"event\":\"intent_parsed\",\"status\":\"ok\",\"message\":null,\"details\":null}\r\n\r\n",
      "event: unknown\r\ndata: ignored\r\n\r\n",
      "event: terminal\r\ndata: {\"status\":\"completed\",\"terminal\":true}\r\n\r\n",
    ];
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
        controller.close();
      },
    });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(stream, { headers: { "content-type": "text/event-stream" } }),
    );
    const events: unknown[] = [];
    const terminals: string[] = [];
    const errors: unknown[] = [];

    streamRunEvents("run/with/slash", 7, (event) => events.push(event), (status) => terminals.push(status), (error) => errors.push(error));
    await vi.waitFor(() => expect(terminals).toEqual(["completed"]));

    expect(events).toEqual([
      {
        seq: 0,
        node: "parse",
        event: "intent_parsed",
        status: "ok",
        message: null,
        details: null,
      },
    ]);
    expect(errors).toEqual([]);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/runs/run%2Fwith%2Fslash/events?after_seq=7",
      expect.objectContaining({
        signal: expect.any(AbortSignal),
        headers: expect.objectContaining({
          get: expect.any(Function),
        }),
      }),
    );
    const request = fetchMock.mock.calls[0]?.[1];
    expect(new Headers(request?.headers).get("X-Admin-Token")).toBe("tab-secret");
    expect(new Headers(request?.headers).get("Accept")).toBe("text/event-stream");
  });

  it("aborts an active stream and downloads DOCX with auth", async () => {
    setAdminToken("tab-secret");
    const encoder = new TextEncoder();
    let resolveStream: (() => void) | undefined;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode(": heartbeat\n\n"));
        resolveStream = () => {
          try {
            controller.close();
          } catch {
            // The consumer may close the reader as part of abort cleanup.
          }
        };
      },
    });
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(stream, { headers: { "content-type": "text/event-stream" } }))
      .mockResolvedValueOnce(new Response(new Uint8Array([0x50, 0x4b]), { status: 200 }));
    const unsubscribe = streamRunEvents("run-1", undefined, vi.fn(), vi.fn(), vi.fn());
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    unsubscribe();
    resolveStream?.();

    const blob = await downloadDocx("run-1");
    expect(blob.size).toBe(2);
    expect(new Headers(fetchMock.mock.calls[1]?.[1]?.headers).get("X-Admin-Token")).toBe("tab-secret");
  });
});
