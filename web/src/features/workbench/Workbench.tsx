import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation } from "@tanstack/react-query";
import { createRun } from "../../api/client";
import { IntentConfirmation } from "./IntentConfirmation";
import { StatusBadge } from "./StatusBadge";

export type RunPhase =
  | "idle"
  | "loading"
  | "awaiting_confirmation"
  | "running"
  | "completed"
  | "failed";

export function Workbench() {
  const [query, setQuery] = useState("");
  const [runId, setRunId] = useState<string | null>(null);
  const [phase, setPhase] = useState<RunPhase>("idle");
  const navigate = useNavigate();

  const mutation = useMutation({
    mutationFn: createRun,
    onSuccess: (run) => {
      setRunId(run.id);
      setPhase("awaiting_confirmation");
    },
    onError: () => setPhase("failed"),
  });

  const handleSubmit = () => {
    if (!query.trim()) return;
    setPhase("loading");
    mutation.mutate(query);
  };

  const handleApprove = () => {
    if (runId) {
      setPhase("running");
      // In the full build this confirms via the API; here we mark complete
      // and navigate to the report.
      setPhase("completed");
      navigate(`/runs/${runId}`);
    }
  };

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

      {phase === "awaiting_confirmation" && runId && (
        <IntentConfirmation runId={runId} onApprove={handleApprove} />
      )}
    </section>
  );
}
