/**
 * The `/?alert=<alert_id>` deep link — where a Teams alert card lands.
 *
 * `backend/teams.py::_alert_link` builds the URL; the Alert Feed page reads the
 * param and opens that alert's drawer, fetching it by id (`GET /alerts/{id}`)
 * rather than looking it up in the loaded list, since the alert may be filtered
 * out, resolved, or older than the first page.
 *
 * The parsing lives here rather than in the page because `npm test` globs `lib/**`
 * and Node cannot strip JSX — same reason as `lib/alertFilters.ts`. Everything in
 * this module is pure.
 */

/**
 * The query-param name, shared so the producer of the link and the reader of it
 * cannot drift. Mirrors the literal in `backend/teams.py::_alert_link`.
 */
export const ALERT_PARAM = "alert";

/**
 * The alert id from a `?alert=` param, or `null` when there is nothing usable.
 *
 * Validated, not merely read: this value arrives from a URL that anyone can edit,
 * and it is interpolated into an API path. Rejecting junk here means the page
 * simply does not open a drawer, instead of firing a request that is guaranteed to
 * 404 (or worse, one whose shape wasn't intended).
 *
 * Accepts what an `alert_id` actually is — a UUID from `ProcessedAlert.alert_id`
 * (`shared/models.py`) — plus the short `al-1`-style ids used in fixtures and
 * tests. Concretely: a non-empty string of `[A-Za-z0-9._-]`, capped in length. A
 * `/`, a space, a `?` or a `%` is therefore refused rather than escaped, because
 * no real alert id contains one.
 *
 * `null` covers all the "no drawer" cases uniformly — param absent, empty, blank,
 * repeated (`URLSearchParams.get` takes the first), or malformed.
 */
export function alertIdFromParam(raw: string | null | undefined): string | null {
  if (!raw) return null;
  const id = raw.trim();
  if (!id || id.length > 128) return null;
  return /^[A-Za-z0-9._-]+$/.test(id) ? id : null;
}
