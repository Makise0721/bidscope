/** Typed API client for the BidScope backend. */

import {
  buildAuthHeaders,
  clearAdminToken,
  notifyUnauthorized,
  UnauthorizedError,
} from "../auth/adminToken";

export const API_BASE = "";

export async function createRun(userRequest: string): Promise<RunRecord> {
  return requestJson<RunRecord>(`${API_BASE}/api/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_request: userRequest }),
  });
}

export async function getRun(id: string): Promise<RunRecord> {
  return requestJson<RunRecord>(`${API_BASE}/api/runs/${id}`);
}

export async function requestJson<T>(url: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: buildAuthHeaders(init.headers),
  });
  await assertSuccessful(response);
  return response.json() as Promise<T>;
}

export async function getRuns(status?: string): Promise<RunRecord[]> {
  const query = status ? `?status=${encodeURIComponent(status)}` : "";
  const response = await requestJson<{ items: RunRecord[] }>(`${API_BASE}/api/runs${query}`);
  return response.items;
}

export async function retryRun(id: string): Promise<RunRecord> {
  return requestJson<RunRecord>(`${API_BASE}/api/runs/${id}/retry`, { method: "POST" });
}

export async function getSubscriptions(): Promise<SubscriptionRecord[]> {
  const response = await requestJson<SubscriptionRecord[] | { items: SubscriptionRecord[] }>(
    `${API_BASE}/api/subscriptions`,
  );
  return Array.isArray(response) ? response : response.items;
}

export async function updateSubscriptionStatus(
  id: string,
  action: "pause" | "resume",
): Promise<SubscriptionRecord> {
  return requestJson<SubscriptionRecord>(`${API_BASE}/api/subscriptions/${id}/${action}`, {
    method: "POST",
  });
}

export async function getInboxEvents(): Promise<InboxEventRecord[]> {
  const response = await requestJson<{ items: InboxEventRecord[] }>(`${API_BASE}/api/inbox-events`);
  return response.items;
}

export async function getSources(): Promise<SourceRecord[]> {
  const response = await requestJson<{ items: SourceRecord[] }>(`${API_BASE}/api/sources`);
  return response.items;
}

export async function getSourceStatuses(): Promise<SourceStatusRecord[]> {
  const response = await requestJson<{ items: SourceStatusRecord[] }>(
    `${API_BASE}/api/sources/status`,
  );
  return response.items;
}

export async function getSourceAcquisitionRuns(
  source?: string,
  page = 1,
  pageSize = 20,
): Promise<SourceAcquisitionHistory> {
  const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  if (source) query.set("source", source);
  return requestJson<SourceAcquisitionHistory>(
    `${API_BASE}/api/sources/acquisition-runs?${query.toString()}`,
  );
}

export async function getEvaluations(): Promise<EvaluationRecord[]> {
  const response = await requestJson<{ items: EvaluationRecord[] }>(`${API_BASE}/api/evaluations`);
  return response.items;
}

export async function confirmRun(id: string): Promise<RunRecord> {
  return requestJson<RunRecord>(`${API_BASE}/api/runs/${id}/confirm`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "approve" }),
  });
}

export async function getReport(id: string): Promise<ReportRecord> {
  return requestJson<ReportRecord>(`${API_BASE}/api/reports/${id}`);
}

/**
 * Subscribe to the run-events SSE stream with an authenticated fetch stream.
 * The callback is invoked asynchronously for HTTP, decoding, and stream errors.
 * Malformed SSE payloads remain best-effort and are ignored.
 */
export function streamRunEvents(
  runId: string,
  afterSeq: number | undefined,
  onEvent: (event: RunEvent) => void,
  onTerminal: (status: string) => void,
  onError: (error: unknown) => void,
): () => void {
  const base = `${API_BASE}/api/runs/${encodeURIComponent(runId)}/events`;
  const url = afterSeq !== undefined ? `${base}?after_seq=${encodeURIComponent(afterSeq)}` : base;
  const controller = new AbortController();
  let stopped = false;
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;

  const stop = () => {
    if (stopped) return;
    stopped = true;
    controller.abort();
    void reader?.cancel();
  };

  void consumeEventStream(
    url,
    controller,
    (nextReader) => {
      reader = nextReader;
    },
    (event) => {
      if (!stopped) onEvent(event);
    },
    (status) => {
      if (stopped) return;
      stopped = true;
      onTerminal(status);
      controller.abort();
      void reader?.cancel();
    },
    (error) => {
      if (!stopped) onError(error);
    },
  );

  return stop;
}

async function consumeEventStream(
  url: string,
  controller: AbortController,
  setReader: (reader: ReadableStreamDefaultReader<Uint8Array>) => void,
  onEvent: (event: RunEvent) => void,
  onTerminal: (status: string) => void,
  onError: (error: unknown) => void,
): Promise<void> {
  try {
    const response = await fetch(url, {
      headers: buildAuthHeaders({ Accept: "text/event-stream" }),
      signal: controller.signal,
    });
    if (response.status === 401) {
      clearAdminToken();
      notifyUnauthorized();
      throw new UnauthorizedError();
    }
    if (!response.ok) {
      throw new Error(`streamRunEvents failed: ${response.status}`);
    }
    if (!response.body) {
      throw new Error("streamRunEvents failed: empty response body");
    }

    const reader = response.body.getReader();
    setReader(reader);
    const decoder = new TextDecoder();
    let buffer = "";
    let done = false;

    while (!done) {
      const result = await reader.read();
      done = result.done;
      if (result.value) buffer += decoder.decode(result.value, { stream: !done });
      for (;;) {
        const frameEnd = findSseFrameEnd(buffer);
        if (frameEnd === -1) break;
        const [frame, remaining] = splitSseFrame(buffer, frameEnd);
        buffer = remaining;
        if (dispatchSseFrame(frame, onEvent, onTerminal)) return;
      }
    }

    buffer += decoder.decode();
    if (buffer.trim()) {
      dispatchSseFrame(buffer, onEvent, onTerminal);
    }
  } catch (error) {
    if (!isAbortError(error)) onError(error);
  }
}

function findSseFrameEnd(buffer: string): number {
  for (let index = 0; index < buffer.length; index += 1) {
    if (buffer[index] === "\n" && buffer[index + 1] === "\n") return index + 2;
    if (buffer[index] === "\r" && buffer[index + 1] === "\n" && buffer[index + 2] === "\r" && buffer[index + 3] === "\n") {
      return index + 4;
    }
  }
  return -1;
}

function splitSseFrame(buffer: string, frameEnd: number): [string, string] {
  const separatorLength = buffer[frameEnd - 4] === "\r" ? 4 : 2;
  return [buffer.slice(0, frameEnd - separatorLength), buffer.slice(frameEnd)];
}

function dispatchSseFrame(
  frame: string,
  onEvent: (event: RunEvent) => void,
  onTerminal: (status: string) => void,
): boolean {
  let eventType = "message";
  const data: string[] = [];
  for (const line of frame.split(/\r\n|\n|\r/)) {
    if (!line || line.startsWith(":")) continue;
    const separator = line.indexOf(":");
    const field = separator === -1 ? line : line.slice(0, separator);
    const value = separator === -1 ? "" : line.slice(separator + 1).replace(/^ /, "");
    if (field === "event") eventType = value;
    if (field === "data") data.push(value);
  }
  if (!data.length) return false;

  const payload = data.join("\n");
  try {
    if (eventType === "terminal") {
      const terminal = JSON.parse(payload) as { status?: unknown; terminal?: unknown };
      if (typeof terminal.status === "string") {
        onTerminal(terminal.status);
        return true;
      }
      return false;
    }
    if (eventType !== "message" && !RUN_EVENT_TYPES.includes(eventType as (typeof RUN_EVENT_TYPES)[number])) {
      return false;
    }
    onEvent(JSON.parse(payload) as RunEvent);
  } catch {
    /* Ignore malformed payloads; SSE is best-effort. */
  }
  return false;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

async function assertSuccessful(response: Response): Promise<void> {
  if (response.ok) return;
  if (response.status === 401) {
    clearAdminToken();
    notifyUnauthorized();
    throw new UnauthorizedError();
  }
  throw new Error(`request failed: ${response.status}`);
}

export async function downloadDocx(id: string): Promise<Blob> {
  const response = await fetch(`${API_BASE}/api/reports/${encodeURIComponent(id)}/docx`, {
    headers: buildAuthHeaders(),
  });
  await assertSuccessful(response);
  return response.blob();
}

export const RUN_EVENT_TYPES = [
  "intent_parsed",
  "intent_valid",
  "intent_invalid",
  "needs_confirmation",
  "run_completed",
  "run_failed",
  "evidence_insufficient",
] as const;

export interface RunRecord {
  id: string;
  status: string;
  request_preview?: string;
  user_request?: string;
  retryable?: boolean;
}

export interface SubscriptionRecord {
  id: string;
  status: string;
  cron_expression: string;
  next_run_at?: string | null;
  last_successful_run_at?: string | null;
}

export interface InboxEventRecord {
  id: string;
  event_type: string;
  title: string | null;
  message?: string;
  read: boolean;
}

export interface SourceBundleRecord {
  bundle_id: string;
  file_identity?: string;
  capture_kind: string;
  source_urls?: string[];
  retrieved_at: string | null;
  hash_prefix: string | null;
  parser_version: string;
  age_days?: number | null;
}

export interface SourceRecord {
  source: string;
  status: string;
  latest_valid_bundle: SourceBundleRecord | null;
  validation_warnings: string[];
}

export interface SourceStatusCounts {
  requests: number;
  records: number;
  new_bundles: number;
  imported_notices: number;
}

export interface SourceStatusRecord {
  source: string;
  status: "healthy" | "stale" | "rate_limited" | "failed" | "disabled" | string;
  last_success_at: string | null;
  next_run_at: string | null;
  lag_seconds: number | null;
  consecutive_failures: number;
  failure_code: string | null;
  counts: SourceStatusCounts;
}

export interface SourceAcquisitionRunRecord {
  id: string;
  source: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  counts: SourceStatusCounts;
  failure_code: string | null;
  http_status: number | null;
  retry_after_seconds: number | null;
}

export interface SourceAcquisitionHistory {
  items: SourceAcquisitionRunRecord[];
  page: number;
  page_size: number;
  has_more: boolean;
}

export interface EvaluationMetric {
  measured: number | null;
  target: number | null;
}

export interface EvaluationRecord {
  id: string;
  dataset_version: string;
  model: string;
  status?: string;
  environment: string | null;
  pricing_snapshot_date: string | null;
  metrics: Record<string, EvaluationMetric>;
}

export interface ReportProvenance {
  source?: string;
  source_title?: string;
  source_url?: string;
  /** Carries "synthetic_demo" / "synthetic_fixture" for synthetic data. */
  capture_kind?: string;
  source_version_id?: string;
  parser_version?: string;
}

export interface ReportCitation {
  evidence_id: string;
  span_hash: string;
  start?: number | null;
  end?: number | null;
  excerpt: string;
  label?: string;
}

export interface ReportClaim {
  text: string;
  citation_ids: string[];
  /** Semantic Citation Contract status; absent on legacy unverified claims. */
  support_status?: string;
}

/** Full Semantic Citation Contract verification record (§4), audit/review only. */
export interface ClaimVerification {
  status: string;
  rationale: string;
  evidence_ids_used: string[];
  conflict_evidence_ids: string[];
  verifier_version: string;
}

/** A claim excluded from the main list (UNSUPPORTED/UNCERTAIN) with its verdict. */
export interface ReviewClaim {
  text: string;
  citation_ids: string[];
  support_status: string;
  verification?: ClaimVerification | null;
}

export interface ReportItem {
  title: string;
  /** Source channel (ccgp/ggzy/synthetic_demo/...). Optional on stripped DTOs. */
  source?: string;
  url?: string;
  retrieved_at?: string;
  hash_prefix?: string;
  /** Backend DTO returns this as a string; legacy fixtures may use number. */
  freshness_days?: string | number;
  /** Allowlisted structured fields from the report DTO. */
  known_fields?: Record<string, string>;
  unknown_fields?: string[];
  relevance_reason?: string;
  risk_note?: string;
  provenance?: ReportProvenance;
  citations?: ReportCitation[];
  claims?: ReportClaim[];
  /** Claims filtered out of ``claims`` (UNSUPPORTED/UNCERTAIN), for review. */
  review_claims?: ReviewClaim[];
}

export interface ReportRecord {
  id: string;
  run_id: string;
  export_key?: string;
  conditions: Record<string, string>;
  freshness_window?: string | null;
  source_availability?: string[];
  completeness_warning?: string | null;
  generated_at?: string | null;
  items: ReportItem[];
}

/** SSE run-event payload shape (matches backend `_event_payload`). */
export interface RunEvent {
  seq: number;
  node: string;
  event: string;
  status: string;
  message: string | null;
  details: Record<string, unknown> | null;
}
