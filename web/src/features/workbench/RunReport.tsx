import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Download } from "lucide-react";
import { getReport, docxUrl } from "../../api/client";
import { isAllowedSourceUrl } from "../../api/sourceUrl";

export function RunReport() {
  const { runId } = useParams<{ runId: string }>();
  const { data: report, isLoading } = useQuery({
    queryKey: ["report", runId],
    queryFn: () => getReport(runId!),
    enabled: Boolean(runId),
  });

  if (isLoading) {
    return <p className="status status-loading">Loading report…</p>;
  }
  if (!report) {
    return <p className="status status-empty">No report.</p>;
  }

  return (
    <div className="report" role="region" aria-label="report">
      <div className="report-header">
        <h2>Report</h2>
        <a
          className="icon-button"
          href={docxUrl(runId!)}
          aria-label="Download DOCX"
        >
          <Download aria-hidden="true" />
          Download DOCX
        </a>
      </div>
      <ul className="opportunity-list">
        {report.items.map((item) => (
          <li key={item.title} className="opportunity">
            <OpportunityItem item={item} />
          </li>
        ))}
      </ul>
    </div>
  );
}

function OpportunityItem({ item }: { item: { title: string; source: string; url: string; retrieved_at?: string; hash_prefix?: string; freshness_days?: number } }) {
  const isSynthetic = item.source === "synthetic_demo";
  return (
    <div className="opportunity-body">
      <h3>{item.title}</h3>
      {isSynthetic && (
        <span className="synthetic-label" data-testid="synthetic-label">
          合成演示数据 (synthetic demo)
        </span>
      )}
      {item.retrieved_at && <p>Retrieved: {item.retrieved_at}</p>}
      {item.hash_prefix && <p>Hash: {item.hash_prefix}…</p>}
      {item.freshness_days !== undefined && (
        <p>Freshness: {item.freshness_days} day(s)</p>
      )}
      {isAllowedSourceUrl(item.source, item.url) ? (
        <a href={item.url} rel="noreferrer">
          {item.url}
        </a>
      ) : (
        <span className="plain-url">{item.url}</span>
      )}
    </div>
  );
}
