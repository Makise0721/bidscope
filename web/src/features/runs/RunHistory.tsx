import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getRuns, retryRun } from "../../api/client";

export function RunHistory() {
  const [status, setStatus] = useState("");
  const queryClient = useQueryClient();
  const runs = useQuery({
    queryKey: ["runs"],
    queryFn: () => getRuns(),
  });
  const visibleRuns = runs.data?.filter((run) => !status || run.status === status) ?? [];
  const retry = useMutation({
    mutationFn: retryRun,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["runs"] }),
  });

  return (
    <section className="operations-page" aria-labelledby="run-history-heading">
      <div className="operations-heading">
        <div>
          <p className="eyebrow">Operations</p>
          {runs.data ? <h1 id="run-history-heading">Run history</h1> : <p className="page-loading">Loading run history...</p>}
        </div>
        <label className="filter-control">
          <span>Status</span>
          <select value={status} onChange={(event) => setStatus(event.target.value)} aria-label="Status">
            <option value="">All statuses</option>
            <option value="retryable">Retryable</option>
            <option value="completed">Completed</option>
            <option value="failed">Failed</option>
          </select>
        </label>
      </div>

      {runs.isLoading && <p className="status">Loading runs...</p>}
      {runs.isError && <p className="status status-error">Unable to load runs.</p>}
      {runs.data && (
        <div className="table-wrap">
          <table className="operations-table">
            <thead>
              <tr>
                <th scope="col">Request</th>
                <th scope="col">Status</th>
                <th scope="col"><span className="visually-hidden">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {visibleRuns.map((run) => (
                <tr key={run.id}>
                  <td>{run.user_request}</td>
                  <td><span className={`status-label status-${run.status}`}>{run.status}</span></td>
                  <td className="table-action">
                    <button
                      type="button"
                      className="secondary-button"
                      onClick={() => retry.mutate(run.id)}
                      disabled={!run.retryable || retry.isPending}
                      aria-label={`Retry ${run.id}`}
                    >
                      Retry
                    </button>
                  </td>
                </tr>
              ))}
              {visibleRuns.length === 0 && (
                <tr><td colSpan={3} className="empty-state">No runs match this filter.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
