import type { ProcessedAlert } from "@/lib/types";

export interface OrderGroup {
  /** Display label: order_id, or event_id when the order was never created
   * (a pre-creation failure, e.g. INBOUND_TRANSFORM_FAILED/ORDER_CREATION_FAILED). */
  label: string;
  /** The journey these alerts belong to — null only in the degenerate case of
   * an alert with no journey_id at all (shouldn't happen for an already-
   * clustered incident, but grouping must not silently drop such an alert). */
  journeyId: string | null;
  /** The representative "what happened to this order" line: the LAST
   * ERROR-level alert in the group, or the last alert overall if the group
   * has no ERROR. NOT simply "last by emitted_at" — that field is when the AI
   * service finished PROCESSING the alert (LLM call / cache lookup), not the
   * original log's own timestamp, and alerts are processed concurrently
   * (ALERT_CONCURRENCY), so a WARN can finish processing after a
   * chronologically-later terminal ERROR. Preferring the last ERROR mirrors
   * the same "ERROR outranks WARN" rule the backend's causal-line picker
   * already uses (first ERROR, else first WARN) — just applied to the last
   * alert instead of the first. */
  outcome: ProcessedAlert;
  /** All of this group's alerts, chronological (emitted_at ascending). */
  alerts: ProcessedAlert[];
}

/** The last ERROR-level alert in `sorted`, or its last alert if none is ERROR. */
function _pickOutcome(sorted: ProcessedAlert[]): ProcessedAlert {
  for (let i = sorted.length - 1; i >= 0; i--) {
    if (sorted[i].level === "ERROR") return sorted[i];
  }
  return sorted[sorted.length - 1];
}

/**
 * Groups an incident's flat alert list into one group per affected order.
 *
 * Grouping key is `journey_id` (reliable — every alert in an already-formed
 * incident belongs to exactly one journey). `order_id`/`event_id` are used
 * only for the display `label`, never for grouping, since an order-specific
 * incident's alerts may have no `order_id` at all yet.
 *
 * Groups are returned most-recently-affected-order first (by each group's
 * own `outcome.emitted_at`, descending).
 */
export function groupAlertsByOrder(alerts: ProcessedAlert[]): OrderGroup[] {
  const byJourney = new Map<string, ProcessedAlert[]>();
  const noJourney: ProcessedAlert[] = [];

  for (const alert of alerts) {
    if (!alert.journey_id) {
      noJourney.push(alert);
      continue;
    }
    const existing = byJourney.get(alert.journey_id);
    if (existing) {
      existing.push(alert);
    } else {
      byJourney.set(alert.journey_id, [alert]);
    }
  }

  const groups: OrderGroup[] = [];

  for (const [journeyId, groupAlerts] of byJourney) {
    const sorted = [...groupAlerts].sort(
      (a, b) => Date.parse(a.emitted_at) - Date.parse(b.emitted_at)
    );
    const outcome = _pickOutcome(sorted);
    groups.push({
      label: outcome.order_id ?? outcome.event_id ?? outcome.alert_id,
      journeyId,
      outcome,
      alerts: sorted,
    });
  }

  // Degenerate case: an alert with no journey_id yet. Each becomes its own
  // singleton group rather than being dropped.
  for (const alert of noJourney) {
    groups.push({
      label: alert.order_id ?? alert.event_id ?? alert.alert_id,
      journeyId: null,
      outcome: alert,
      alerts: [alert],
    });
  }

  groups.sort((a, b) => Date.parse(b.outcome.emitted_at) - Date.parse(a.outcome.emitted_at));
  return groups;
}
