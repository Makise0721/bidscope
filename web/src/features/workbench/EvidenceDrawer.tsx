import { useEffect } from "react";
import type { ReportItem } from "../../api/client";
import { isAllowedSourceUrl } from "../../api/sourceUrl";

interface EvidenceDrawerProps {
  item: ReportItem;
  onClose: () => void;
}

/**
 * Slide-over drawer that surfaces the persisted evidence for a single report
 * item: provenance (capture kind → synthetic label, source, retrieval time,
 * hash prefix), citation spans, and claims. Each opportunity item in
 * ``RunReport`` opens this drawer via its own "Open evidence" button; the
 * Workbench additionally renders a desktop trace column (``RunTimeline``) that
 * is independent of this drawer.
 *
 * Accessibility note: the drawer provides ``role="dialog"``, ``aria-modal``,
 * and Escape-to-close. Full keyboard focus management (focus trap + restoring
 * focus to the triggering button on close) is tracked as a follow-up.
 */
export function EvidenceDrawer({ item, onClose }: EvidenceDrawerProps) {
  // Close on Escape for keyboard users.
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  const provenance = item.provenance;
  const captureKind = provenance?.capture_kind;
  const isSynthetic =
    (captureKind !== undefined && captureKind.startsWith("synthetic")) ||
    item.source === "synthetic_demo";
  const sourceChannel = item.source ?? provenance?.source ?? "";
  const url = item.url ?? provenance?.source_url ?? "";
  const citations = item.citations ?? [];
  const citationById = new Map(citations.map((citation) => [citation.evidence_id, citation]));

  return (
    <div
      className="evidence-drawer-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="Evidence detail"
      onClick={onClose}
    >
      <div
        className="evidence-drawer"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="evidence-drawer-header">
          <h3>Evidence</h3>
          <button
            type="button"
            className="secondary-button"
            aria-label="Close evidence"
            onClick={onClose}
          >
            Close
          </button>
        </div>

        <section className="evidence-section">
          <h4>Provenance</h4>
          {isSynthetic && (
            <p className="evidence-synthetic">
              <span className="synthetic-label" data-testid="synthetic-label">
                合成演示数据 (synthetic demo)
              </span>
            </p>
          )}
          <dl className="provenance-detail">
            {sourceChannel && (
              <>
                <dt>Source</dt>
                <dd>{sourceChannel}</dd>
              </>
            )}
            {provenance?.source_title && (
              <>
                <dt>Title</dt>
                <dd>{provenance.source_title}</dd>
              </>
            )}
            {url && (
              <>
                <dt>URL</dt>
                <dd>
                  {isAllowedSourceUrl(sourceChannel, url) ? (
                    <a href={url} rel="noreferrer">
                      {url}
                    </a>
                  ) : (
                    <span className="plain-url">{url}</span>
                  )}
                </dd>
              </>
            )}
            {captureKind && (
              <>
                <dt>Capture kind</dt>
                <dd>{captureKind}</dd>
              </>
            )}
            {provenance?.parser_version && (
              <>
                <dt>Parser</dt>
                <dd>{provenance.parser_version}</dd>
              </>
            )}
            {item.retrieved_at && (
              <>
                <dt>Retrieved</dt>
                <dd>{item.retrieved_at}</dd>
              </>
            )}
            {item.hash_prefix && (
              <>
                <dt>Hash</dt>
                <dd>{item.hash_prefix}…</dd>
              </>
            )}
          </dl>
        </section>

        {citations.length > 0 && (
          <section className="evidence-section">
            <h4>Citations</h4>
            <ul className="citation-list">
              {citations.map((citation) => (
                <li key={citation.evidence_id} className="citation">
                  {citation.label && (
                    <p className="citation-label">{citation.label}</p>
                  )}
                  <p className="citation-excerpt">{citation.excerpt}</p>
                  <p className="citation-meta muted">
                    span {citation.span_hash}
                    {citation.start !== undefined && citation.start !== null
                      ? ` @ ${citation.start}`
                      : ""}
                    {citation.end !== undefined && citation.end !== null
                      ? `–${citation.end}`
                      : ""}
                  </p>
                </li>
              ))}
            </ul>
          </section>
        )}

        {item.claims && item.claims.length > 0 && (
          <section className="evidence-section">
            <h4>Claims</h4>
            <ul className="claim-list">
              {item.claims.map((claim, index) => (
                <li key={index} className="claim">
                  <p>{claim.text}</p>
                  {claim.citation_ids.length > 0 && (
                    <ul className="claim-citations">
                      {claim.citation_ids.map((id) => (
                        <li key={id} className="muted">
                          {citationById.get(id)?.label ?? id}
                        </li>
                      ))}
                    </ul>
                  )}
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </div>
  );
}
