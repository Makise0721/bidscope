import { useQuery } from "@tanstack/react-query";
import { getSources, getSourceStatuses } from "../../api/client";
import { isAllowedSourceUrl } from "../../api/sourceUrl";

function displayLabel(value: string): string {
  return value.replaceAll("_", " ");
}

function statusLabel(value: string): string {
  const labels: Record<string, string> = {
    healthy: "Healthy",
    stale: "Stale",
    rate_limited: "Rate limited",
    failed: "Failed",
    disabled: "Disabled",
  };
  return labels[value] ?? displayLabel(value);
}

function snapshotStatusLabel(value: string): string {
  return value === "stale" ? "outdated" : statusLabel(value);
}

function formatLag(seconds: number | null): string {
  if (seconds === null) return "Unknown";
  if (seconds < 3600) return `${Math.max(1, Math.floor(seconds / 60))} min`;
  return `${(seconds / 3600).toFixed(1)} h`;
}

export function SourcesView() {
  const sources = useQuery({ queryKey: ["sources"], queryFn: getSources });
  const statuses = useQuery({ queryKey: ["source-statuses"], queryFn: getSourceStatuses });
  const statusBySource = new Map((statuses.data ?? []).map((status) => [status.source, status]));

  return (
    <section className="operations-page" aria-labelledby="data-sources-heading">
      <div className="operations-heading">
        <div>
          <p className="eyebrow">Provenance</p>
          {sources.data ? <h1 id="data-sources-heading">Data sources</h1> : <p className="page-loading">Loading data sources...</p>}
        </div>
      </div>
      {sources.isLoading && <p className="status">Loading sources...</p>}
      {(sources.isError || statuses.isError) && <p className="status status-error">Unable to load source status.</p>}
      {sources.data && (
        <div className="table-wrap">
          <table className="operations-table">
            <thead>
              <tr>
                <th scope="col">Source</th>
                <th scope="col">Status</th>
                <th scope="col">Freshness</th>
                <th scope="col">Latest run</th>
                <th scope="col">Latest valid bundle</th>
                <th scope="col">Warnings</th>
              </tr>
            </thead>
            <tbody>
              {sources.data.map((source) => {
                const ingestion = statusBySource.get(source.source);
                return (
                  <tr key={source.source}>
                  <th scope="row">
                    {source.source}
                    {source.source === "synthetic_demo" && (
                      <span className="synthetic-label">合成演示数据 (synthetic demo)</span>
                    )}
                  </th>
                  <td>
                    <span className={`status-label status-${ingestion?.status ?? source.status}`}>
                      {ingestion ? statusLabel(ingestion.status) : snapshotStatusLabel(source.status)}
                    </span>
                    {ingestion?.failure_code && <span className="muted"> {ingestion.failure_code}</span>}
                  </td>
                  <td>
                    {ingestion ? (
                      <div className="provenance-detail">
                        <span>Last success {ingestion.last_success_at ?? "Never"}</span>
                        <span>Lag {formatLag(ingestion.lag_seconds)}</span>
                        <span>Next run {ingestion.next_run_at ?? "Not scheduled"}</span>
                      </div>
                    ) : <span className="muted">Snapshot only</span>}
                  </td>
                  <td>
                    {ingestion ? (
                      <div className="provenance-detail">
                        <span>{ingestion.counts.imported_notices} notices imported</span>
                        <span>{ingestion.counts.records} records received</span>
                        <span>{ingestion.counts.requests} requests</span>
                      </div>
                    ) : <span className="muted">No acquisition run</span>}
                  </td>
                  <td>
                    {source.latest_valid_bundle ? (
                      <div className="provenance-detail">
                        <strong>{displayLabel(source.latest_valid_bundle.capture_kind)}</strong>
                        <span>{source.latest_valid_bundle.parser_version}</span>
                        <span>Retrieved {source.latest_valid_bundle.retrieved_at ?? "Unknown"}</span>
                        <span>Age {source.latest_valid_bundle.age_days ?? "Unknown"} day(s)</span>
                        <span>Identity {source.latest_valid_bundle.file_identity ?? source.latest_valid_bundle.bundle_id}</span>
                        <span>{source.latest_valid_bundle.hash_prefix ?? "No hash"}</span>
                        {source.latest_valid_bundle.source_urls?.map((url) => (
                          isAllowedSourceUrl(source.source, url) ? (
                            <a href={url} key={url} rel="noreferrer">{url}</a>
                          ) : (
                            <span className="plain-url" key={url}>{url}</span>
                          )
                        ))}
                      </div>
                    ) : <span className="muted">None</span>}
                  </td>
                  <td>
                    {source.validation_warnings.length > 0 ? (
                      <ul className="warning-list">
                        {source.validation_warnings.map((warning) => <li key={warning}>{warning}</li>)}
                      </ul>
                    ) : <span className="muted">None</span>}
                  </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
