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
  user_request: string;
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
