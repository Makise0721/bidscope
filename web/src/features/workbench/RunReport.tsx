import { useState } from "react";
import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Download } from "lucide-react";
import { getReport, docxUrl } from "../../api/client";
import type { ReportItem, ReportRecord } from "../../api/client";
import { isAllowedSourceUrl } from "../../api/sourceUrl";
import { EvidenceDrawer } from "./EvidenceDrawer";

/**
 * Route-bound report view at ``/runs/:runId``. Delegates rendering to the
 * presentational ``RunReport`` once the report is loaded.
 */
export function RunReportRoute() {
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

  return <RunReport report={report} runIdForDownload={runId} />;
}

interface RunReportProps {
  report: ReportRecord;
  /**
   * Run id used to build the DOCX download URL. Optional so this component can
   * be rendered in isolation (e.g. in tests or inline in the workbench) where
   * the report id alone is enough.
   */
  runIdForDownload?: string;
}

/**
 * Presentational report view. Renders the report header (with DOCX download
 * when a run id is available), the opportunity list, and an "Open evidence"
 * trigger that surfaces the citation/provenance drawer.
 */
export function RunReport({ report, runIdForDownload }: RunReportProps) {
  const [openItem, setOpenItem] = useState<number | null>(null);
  const downloadId = runIdForDownload ?? report.run_id;

  return (
    <div className="report" role="region" aria-label="report">
      <div className="report-header">
        <h2>Report</h2>
        <a
          className="icon-button"
          href={docxUrl(downloadId)}
          aria-label="Download DOCX"
        >
          <Download aria-hidden="true" />
          Download DOCX
        </a>
      </div>
      {report.completeness_warning && (
        <p className="status status-warning" role="status">
          {report.completeness_warning}
        </p>
      )}
      <ul className="opportunity-list">
        {report.items.map((item, index) => (
          <li key={`${item.title}-${index}`} className="opportunity">
            <OpportunityItem
              item={item}
              onOpenEvidence={() => setOpenItem(index)}
            />
          </li>
        ))}
      </ul>
      {openItem !== null && report.items[openItem] && (
        <EvidenceDrawer
          item={report.items[openItem]}
          onClose={() => setOpenItem(null)}
        />
      )}
    </div>
  );
}

interface OpportunityItemProps {
  item: ReportItem;
  onOpenEvidence: () => void;
}

function OpportunityItem({ item, onOpenEvidence }: OpportunityItemProps) {
  // Synthetic signal lives on provenance.capture_kind in the richer DTO; fall
  // back to the legacy item.source marker so older fixtures keep working.
  const captureKind = item.provenance?.capture_kind;
  const isSynthetic =
    (captureKind !== undefined && captureKind.startsWith("synthetic")) ||
    item.source === "synthetic_demo";

  const sourceChannel = item.source ?? item.provenance?.source ?? "";
  const url = item.url ?? item.provenance?.source_url ?? "";

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
      {url &&
        (isAllowedSourceUrl(sourceChannel, url) ? (
          <a href={url} rel="noreferrer">
            {url}
          </a>
        ) : (
          <span className="plain-url">{url}</span>
        ))}
      <button type="button" className="secondary-button" onClick={onOpenEvidence}>
        Open evidence
      </button>
    </div>
  );
}

