/** Typed API client for the BidScope backend. */

export const API_BASE = "";

export async function createRun(userRequest: string): Promise<RunRecord> {
  const response = await fetch(`${API_BASE}/api/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_request: userRequest }),
  });
  if (!response.ok) {
    throw new Error(`createRun failed: ${response.status}`);
  }
  return response.json();
}

export async function getRun(id: string): Promise<RunRecord> {
  const response = await fetch(`${API_BASE}/api/runs/${id}`);
  if (!response.ok) {
    throw new Error(`getRun failed: ${response.status}`);
  }
  return response.json();
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    throw new Error(`request failed: ${response.status}`);
  }
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

export async function getEvaluations(): Promise<EvaluationRecord[]> {
  const response = await requestJson<{ items: EvaluationRecord[] }>(`${API_BASE}/api/evaluations`);
  return response.items;
}

export async function confirmRun(id: string): Promise<RunRecord> {
  const response = await fetch(`${API_BASE}/api/runs/${id}/confirm`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "approve" }),
  });
  if (!response.ok) {
    throw new Error(`confirmRun failed: ${response.status}`);
  }
  return response.json();
}

export async function getReport(id: string): Promise<ReportRecord> {
  const response = await fetch(`${API_BASE}/api/reports/${id}`);
  if (!response.ok) {
    throw new Error(`getReport failed: ${response.status}`);
  }
  return response.json();
}

/**
 * Subscribe to the run-events SSE stream.
 *
 * Returns an unsubscribe function that closes the underlying EventSource.
 *
 * SSE reconciliation note: the backend always emits frames with an explicit
 * ``event:`` field (e.g. ``event: intent_parsed``). Per the SSE spec, a frame
 * with an ``event:`` field dispatches a *typed* event on EventSource and does
 * NOT trigger ``onmessage`` (which only fires for unnamed frames). We therefore
 * register an explicit listener per known node event name in addition to the
 * ``terminal`` listener. The ``?after_seq=`` query param is supported by the
 * backend route so browser EventSource (which cannot set Last-Event-ID) can
 * resume after a reconnect.
 */
export function streamRunEvents(
  runId: string,
  afterSeq: number | undefined,
  onEvent: (event: RunEvent) => void,
  onTerminal: (status: string) => void,
): () => void {
  const base = `${API_BASE}/api/runs/${encodeURIComponent(runId)}/events`;
  const url = afterSeq !== undefined ? `${base}?after_seq=${afterSeq}` : base;
  const source = new EventSource(url);
  const dispatch = (message: MessageEvent) => {
    try {
      onEvent(JSON.parse(message.data) as RunEvent);
    } catch {
      /* ignore malformed payloads; SSE is best-effort */
    }
  };
  // Unnamed frames (e.g. future "data:" only heartbeats) hit onmessage.
  source.onmessage = dispatch;
  // Typed node events from the BidScope graph. Keep this list in sync with the
  // backend event vocabulary; unknown event names are simply ignored here.
  for (const type of RUN_EVENT_TYPES) {
    source.addEventListener(type, dispatch as EventListener);
  }
  source.addEventListener(
    "terminal",
    ((message: MessageEvent) => {
      try {
        const payload = JSON.parse(message.data) as { status: string };
        onTerminal(payload.status);
      } catch {
        /* ignore */
      }
      source.close();
    }) as EventListener,
  );
  source.onerror = () => source.close();
  return () => source.close();
}

/**
 * Node-event names emitted by the BidScope graph that the workbench timeline
 * surfaces. Limited to the names the backend persists today so we register a
 * bounded set of typed listeners on EventSource.
 */
export const RUN_EVENT_TYPES = [
  "intent_parsed",
  "intent_valid",
  "intent_invalid",
  "needs_confirmation",
  "run_completed",
  "run_failed",
  "evidence_insufficient",
] as const;

export function docxUrl(id: string): string {
  return `${API_BASE}/api/reports/${id}/docx`;
}

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
