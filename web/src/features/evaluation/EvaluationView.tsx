import { useQuery } from "@tanstack/react-query";
import { getEvaluations } from "../../api/client";

function formatMetric(value: number | null): string {
  return value === null ? "Not measured" : value.toFixed(2);
}

export function EvaluationView() {
  const evaluations = useQuery({ queryKey: ["evaluations"], queryFn: getEvaluations });

  return (
    <section className="operations-page" aria-labelledby="evaluation-heading">
      <div className="operations-heading">
        <div>
          <p className="eyebrow">Quality</p>
          {evaluations.data ? <h1 id="evaluation-heading">Evaluation</h1> : <p className="page-loading">Loading evaluation...</p>}
        </div>
      </div>
      {evaluations.isLoading && <p className="status">Loading evaluations...</p>}
      {evaluations.isError && <p className="status status-error">Unable to load evaluations.</p>}
      <div className="evaluation-list">
        {evaluations.data?.map((evaluation) => (
          <article className="evaluation-card" key={evaluation.id}>
            <div className="evaluation-card-header">
              <div>
                <h2>{evaluation.dataset_version}</h2>
                <p>{evaluation.model}</p>
              </div>
              <span className="status-label">{evaluation.environment ?? "Unknown environment"}</span>
            </div>
            {evaluation.pricing_snapshot_date && (
              <p className="muted">Pricing snapshot {evaluation.pricing_snapshot_date}</p>
            )}
            <div className="metric-grid">
              {Object.entries(evaluation.metrics).map(([name, metric]) => (
                <div className="metric-card" key={name}>
                  <strong>{name}</strong>
                  <dl>
                    <div><dt>Measured</dt><dd>{formatMetric(metric.measured)}</dd></div>
                    <div><dt>Target</dt><dd>{formatMetric(metric.target)}</dd></div>
                  </dl>
                </div>
              ))}
            </div>
          </article>
        ))}
      </div>
      {evaluations.data?.length === 0 && <p className="empty-state">No evaluation runs recorded.</p>}
    </section>
  );
}
