import { useEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import {
  confirmRun,
  createRun,
  getReport,
  streamRunEvents,
} from "../../api/client";
import type { ReportRecord, RunEvent } from "../../api/client";
import {
  getAdminToken,
  onAdminTokenChange,
  onUnauthorized,
  UnauthorizedError,
} from "../../auth/adminToken";
import { IntentConfirmation } from "./IntentConfirmation";
import { RunReport } from "./RunReport";
import { RunTimeline } from "./RunTimeline";
import { StatusBadge } from "./StatusBadge";

export type RunPhase =
  | "idle"
  | "loading"
  | "auth_needed"
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

  // Track the active stream so auth failures and phase changes close it promptly.
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

  useEffect(() => {
    const unsubscribeToken = onAdminTokenChange(() => {
      if (getAdminToken()) setPhase((current) => (current === "auth_needed" ? "idle" : current));
    });
    const unsubscribeUnauthorized = onUnauthorized(() => {
      stopSubscription();
      setPhase("auth_needed");
    });
    return () => {
      unsubscribeToken();
      unsubscribeUnauthorized();
    };
  }, []);

  const handleRequestError = (error: unknown) => {
    stopSubscription();
    setPhase(error instanceof UnauthorizedError ? "auth_needed" : "failed");
  };

  const mutation = useMutation({
    mutationFn: createRun,
    onSuccess: (run) => {
      setRunId(run.id);
      const nextPhase = phaseFromStatus(run.status);
      setPhase(nextPhase);
      if (nextPhase === "completed") {
        void getReport(run.id).then(setReport).catch(handleRequestError);
      } else if (nextPhase === "running") {
        subscribeToEvents(run.id);
      }
    },
    onError: handleRequestError,
  });

  const confirmationMutation = useMutation({
    mutationFn: confirmRun,
    onSuccess: (run) => {
      const nextPhase = phaseFromStatus(run.status);
      setPhase(nextPhase);
      if (nextPhase === "completed") {
        void getReport(run.id).then(setReport).catch(handleRequestError);
      } else if (nextPhase === "running") {
        subscribeToEvents(run.id);
      }
    },
    onError: handleRequestError,
  });

  /** Subscribe to progress and fetch the report after a completed terminal event. */
  const subscribeToEvents = (id: string) => {
    stopSubscription();
    setEvents([]);
    const unsubscribe = streamRunEvents(
      id,
      undefined,
      (event) => {
        setEvents((previous) => {
          if (previous.some((existing) => existing.seq === event.seq)) return previous;
          return [...previous, event].sort((a, b) => a.seq - b.seq);
        });
      },
      (status) => {
        const terminalPhase =
          status === "completed"
            ? "completed"
            : status === "awaiting_confirmation"
              ? "awaiting_confirmation"
              : "failed";
        setPhase(terminalPhase);
        if (status === "completed") {
          void getReport(id).then(setReport).catch(handleRequestError);
        }
        stopSubscription();
      },
      handleRequestError,
    );
    unsubscribeRef.current = unsubscribe;
  };

  const handleSubmit = () => {
    if (!query.trim() || phase === "auth_needed") return;
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
          disabled={phase === "loading" || phase === "auth_needed" || !query.trim()}
          aria-label="Search"
        >
          Search
        </button>
      </div>

      <StatusBadge phase={phase} />
      {phase === "auth_needed" && (
        <p className="status status-error" role="alert">
          Authentication required. Enter an Admin Token above to continue.
        </p>
      )}
      {confirmationMutation.isError && phase !== "auth_needed" && (
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
