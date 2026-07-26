import { useEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import {
  confirmRun,
  createRun,
  getReport,
  streamRunEvents,
} from "../../api/client";
import type { ReportRecord, RunEvent } from "../../api/client";
import { IntentConfirmation } from "./IntentConfirmation";
import { RunReport } from "./RunReport";
import { RunTimeline } from "./RunTimeline";
import { StatusBadge } from "./StatusBadge";

export type RunPhase =
  | "idle"
  | "loading"
  | "awaiting_confirmation"
  | "running"
  | "completed"
  | "failed";

function phaseFromStatus(status: string): RunPhase {
  switch (status) {
    case "awaiting_confirmation":
      return "awaiting_confirmation";
    case "completed":
      return "completed";
    case "failed":
    case "retryable":
    case "evidence_insufficient":
      return "failed";
    case "pending":
    case "running":
      return "running";
    default:
      return "running";
  }
}

export function Workbench() {
  const [query, setQuery] = useState("");
  const [runId, setRunId] = useState<string | null>(null);
  const [phase, setPhase] = useState<RunPhase>("idle");
  const [report, setReport] = useState<ReportRecord | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);

  // Track the active SSE unsubscribe so we can tear it down on phase change or
  // unmount. Ref so the cleanup closure stays stable.
  const unsubscribeRef = useRef<(() => void) | null>(null);

  const stopSubscription = () => {
    if (unsubscribeRef.current) {
      unsubscribeRef.current();
      unsubscribeRef.current = null;
    }
  };

  useEffect(() => {
    return () => stopSubscription();
  }, []);

  const mutation = useMutation({
    mutationFn: createRun,
    onSuccess: (run) => {
      setRunId(run.id);
      const nextPhase = phaseFromStatus(run.status);
      setPhase(nextPhase);
      // If the run already reached terminal "completed" without confirmation,
      // fetch the report directly (the workbench does not assume every query
      // requires approval).
      if (nextPhase === "completed") {
        void getReport(run.id).then(setReport).catch(() => setPhase("failed"));
      } else if (nextPhase === "running") {
        subscribeToEvents(run.id);
      }
    },
    onError: () => setPhase("failed"),
  });

  const confirmationMutation = useMutation({
    mutationFn: confirmRun,
    onSuccess: (run) => {
      const nextPhase = phaseFromStatus(run.status);
      setPhase(nextPhase);
      if (nextPhase === "completed") {
        void getReport(run.id).then(setReport).catch(() => setPhase("failed"));
      } else if (nextPhase === "running") {
        subscribeToEvents(run.id);
      }
    },
    onError: () => setPhase("failed"),
  });

  /**
   * Subscribe to the SSE event stream, append ordered node events, and fetch
   * the report on a terminal "completed" status. The subscription is torn down
   * when the run reaches a terminal state.
   */
  const subscribeToEvents = (id: string) => {
    stopSubscription();
    setEvents([]);
    const unsubscribe = streamRunEvents(
      id,
      undefined,
      (event) => {
        setEvents((previous) => {
          // Avoid duplicate seqs on reconnect.
          if (previous.some((existing) => existing.seq === event.seq)) {
            return previous;
          }
          return [...previous, event].sort((a, b) => a.seq - b.seq);
        });
      },
      (status) => {
        const terminalPhase = status === "completed" ? "completed" : "failed";
        setPhase(terminalPhase);
        if (status === "completed") {
          void getReport(id).then(setReport).catch(() => setPhase("failed"));
        }
        stopSubscription();
      },
    );
    unsubscribeRef.current = unsubscribe;
  };

  const handleSubmit = () => {
    if (!query.trim()) return;
    stopSubscription();
    setReport(null);
    setEvents([]);
    setPhase("loading");
    mutation.mutate(query);
  };

  const handleApprove = () => {
    if (runId) {
      stopSubscription();
      setEvents([]);
      setPhase("running");
      confirmationMutation.mutate(runId);
    }
  };

  const showSide = phase === "running" || (phase === "completed" && report !== null);

  return (
    <section className="workbench" aria-label="query workbench">
      <div className="query-row">
        <label htmlFor="query-input" className="visually-hidden">
          Enter your request
        </label>
        <input
          id="query-input"
          className="query-input"
          type="text"
          placeholder="Describe the tenders you are looking for…"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          aria-label="Enter your request"
        />
        <button
          type="button"
          className="icon-button"
          onClick={handleSubmit}
          disabled={phase === "loading" || !query.trim()}
          aria-label="Search"
        >
          Search
        </button>
      </div>

      <StatusBadge phase={phase} />
      {confirmationMutation.isError && (
        <p className="status status-error" role="alert">
          Unable to confirm this run. Please try again.
        </p>
      )}

      <div className="workbench-grid">
        <div className="workbench-main">
          {phase === "awaiting_confirmation" && runId && (
            <IntentConfirmation runId={runId} onApprove={handleApprove} />
          )}
          {phase === "completed" && report && (
            <RunReport report={report} runIdForDownload={runId ?? report.run_id} />
          )}
        </div>

        {showSide && (
          <aside className="workbench-side" aria-label="run trace and evidence">
            <RunTimeline events={events} />
          </aside>
        )}
      </div>
    </section>
  );
}
