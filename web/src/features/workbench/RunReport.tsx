import { useState } from "react";
import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Download } from "lucide-react";
import { downloadDocx, getReport } from "../../api/client";
import type { ReportItem, ReportRecord } from "../../api/client";
import { isAllowedSourceUrl } from "../../api/sourceUrl";
import { EvidenceDrawer } from "./EvidenceDrawer";

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
  runIdForDownload?: string;
}

export function RunReport({ report, runIdForDownload }: RunReportProps) {
  const [openItem, setOpenItem] = useState<number | null>(null);
  const [downloadPending, setDownloadPending] = useState(false);
  const downloadId = runIdForDownload ?? report.run_id;

  const handleDownload = async () => {
    setDownloadPending(true);
    try {
      const blob = await downloadDocx(downloadId);
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = `bidscope-${downloadId}.docx`;
      link.click();
      URL.revokeObjectURL(objectUrl);
    } finally {
      setDownloadPending(false);
    }
  };

  return (
    <div className="report" role="region" aria-label="report">
      <div className="report-header">
        <h2>Report</h2>
        <button
          className="icon-button"
          type="button"
          onClick={() => {
            void handleDownload().catch(() => undefined);
          }}
          disabled={downloadPending}
          aria-label="Download DOCX"
        >
          <Download aria-hidden="true" />
          {downloadPending ? "Preparing DOCX" : "Download DOCX"}
        </button>
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
