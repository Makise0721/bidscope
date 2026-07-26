import type { RunEvent } from "../../api/client";

interface RunTimelineProps {
  events: RunEvent[];
}

/**
 * Ordered rendering of the SSE node-event trace for a run. Each entry shows
 * the node name, event type, and status, surfaced in the workbench's
 * right column on desktop.
 */
export function RunTimeline({ events }: RunTimelineProps) {
  if (events.length === 0) {
    return null;
  }
  return (
    <section className="run-timeline" aria-label="run timeline">
      <h3>Trace</h3>
      <ol className="timeline-list">
        {events.map((event) => (
          <li
            key={`${event.seq}`}
            className={`timeline-item timeline-${event.status}`}
          >
            <span className="timeline-node">{event.node}</span>
            <span className="timeline-event">{event.event}</span>
            {event.message && (
              <span className="timeline-message muted">{event.message}</span>
            )}
          </li>
        ))}
      </ol>
    </section>
  );
}
