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

export interface ReportItem {
  title: string;
  source: string;
  url: string;
  retrieved_at?: string;
  hash_prefix?: string;
  freshness_days?: number;
}

export interface ReportRecord {
  id: string;
  run_id: string;
  conditions: Record<string, string>;
  items: ReportItem[];
}
