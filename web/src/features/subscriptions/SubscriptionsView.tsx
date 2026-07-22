import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  getInboxEvents,
  getSubscriptions,
  updateSubscriptionStatus,
} from "../../api/client";

export function SubscriptionsView() {
  const queryClient = useQueryClient();
  const subscriptions = useQuery({ queryKey: ["subscriptions"], queryFn: getSubscriptions });
  const inbox = useQuery({ queryKey: ["inbox-events"], queryFn: getInboxEvents });
  const statusMutation = useMutation({
    mutationFn: ({ id, action }: { id: string; action: "pause" | "resume" }) =>
      updateSubscriptionStatus(id, action),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["subscriptions"] }),
  });

  return (
    <section className="operations-page" aria-labelledby="subscriptions-heading">
      <div className="operations-heading">
        <div>
          <p className="eyebrow">Operations</p>
          <h1 id="subscriptions-heading">Subscriptions</h1>
        </div>
      </div>

      {statusMutation.isError && (
        <p className="status status-error">Unable to update subscription: {statusMutation.error.message}</p>
      )}
      <div className="operations-grid">
        <section className="operations-section" aria-labelledby="schedules-heading">
          <h2 id="schedules-heading">Schedules</h2>
          {subscriptions.isLoading && <p className="status">Loading subscriptions...</p>}
          {subscriptions.isError && <p className="status status-error">Unable to load subscriptions.</p>}
          {subscriptions.data?.map((subscription) => {
            const paused = subscription.status === "paused";
            return (
              <div className="operation-row" key={subscription.id}>
                <div>
                  <strong>{subscription.cron_expression}</strong>
                  <span className={`status-label status-${subscription.status}`}>{subscription.status}</span>
                  {subscription.next_run_at && <small>Next run {subscription.next_run_at}</small>}
                </div>
                <button
                  type="button"
                  className="secondary-button"
                  onClick={() => statusMutation.mutate({ id: subscription.id, action: paused ? "resume" : "pause" })}
                  disabled={statusMutation.isPending}
                >
                  {paused ? "Resume" : "Pause"}
                </button>
              </div>
            );
          })}
          {subscriptions.data?.length === 0 && <p className="empty-state">No subscriptions configured.</p>}
        </section>

        <section className="operations-section" aria-labelledby="inbox-heading">
          <div className="section-heading-row">
            <h2 id="inbox-heading">Inbox</h2>
            <span className="section-count">{inbox.data?.filter((event) => !event.read).length ?? 0} unread</span>
          </div>
          {inbox.isLoading && <p className="status">Loading inbox...</p>}
          {inbox.isError && <p className="status status-error">Unable to load inbox.</p>}
          <ul className="inbox-list">
            {inbox.data?.map((event) => (
              <li className={`inbox-item ${event.read ? "is-read" : "is-unread"}`} key={event.id}>
                <span className="inbox-dot" aria-hidden="true" />
                <div>
                  <strong>{event.title ?? event.event_type}</strong>
                  {event.message && <span>{event.message}</span>}
                  <span>{event.read ? "Read" : "Unread"}</span>
                </div>
              </li>
            ))}
          </ul>
        </section>
      </div>
    </section>
  );
}
