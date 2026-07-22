import { useQuery } from "@tanstack/react-query";
import { getSources } from "../../api/client";
import { isAllowedSourceUrl } from "../../api/sourceUrl";

function displayLabel(value: string): string {
  return value.replaceAll("_", " ");
}

export function SourcesView() {
  const sources = useQuery({ queryKey: ["sources"], queryFn: getSources });

  return (
    <section className="operations-page" aria-labelledby="data-sources-heading">
      <div className="operations-heading">
        <div>
          <p className="eyebrow">Provenance</p>
          {sources.data ? <h1 id="data-sources-heading">Data sources</h1> : <p className="page-loading">Loading data sources...</p>}
        </div>
      </div>
      {sources.isLoading && <p className="status">Loading sources...</p>}
      {sources.isError && <p className="status status-error">Unable to load sources.</p>}
      {sources.data && (
        <div className="table-wrap">
          <table className="operations-table">
            <thead>
              <tr>
                <th scope="col">Source</th>
                <th scope="col">Status</th>
                <th scope="col">Latest valid bundle</th>
                <th scope="col">Warnings</th>
              </tr>
            </thead>
            <tbody>
              {sources.data.map((source) => (
                <tr key={source.source}>
                  <th scope="row">
                    {source.source}
                    {source.source === "synthetic_demo" && (
                      <span className="synthetic-label">合成演示数据 (synthetic demo)</span>
                    )}
                  </th>
                  <td><span className={`status-label status-${source.status}`}>{source.status === "stale" ? "outdated" : source.status}</span></td>
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
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
