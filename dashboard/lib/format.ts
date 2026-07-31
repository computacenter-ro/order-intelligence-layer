import type { Journey, LogLevel } from "@/lib/types";

const LEVEL_LABEL: Record<LogLevel, string> = {
  DEBUG: "Debug",
  INFO: "Info",
  WARN: "Warning",
  ERROR: "Error",
};

export function levelLabel(level: LogLevel): string {
  return LEVEL_LABEL[level];
}

/** Live "N new …" pills (alerts, incidents) must never show an unbounded
 *  number — a stretch during a burst reads worse than a stale-feeling cap. */
const NOTIFICATION_COUNT_CAP = 20;

export function formatNotificationCount(count: number): string {
  return count > NOTIFICATION_COUNT_CAP ? `${NOTIFICATION_COUNT_CAP}+` : String(count);
}

/**
 * Turn a session identity into a person's name for display:
 * `"larisa.muntean@computacenter.com"` -> `"Larisa Muntean"`.
 *
 * The backend's session identity is whatever Entra put in `preferred_username`
 * (a UPN, i.e. an e-mail), so the raw value is what the sign-out row used to
 * show. Only the display changes — the identity itself is untouched, and callers
 * should keep the full value in a `title`/`aria-label` so nothing is lost.
 *
 * Everything after `@` is dropped, then `.` and `-` become word boundaries and
 * each word is title-cased (hyphens are preserved: `anne-marie` ->
 * `Anne-Marie`). Values with no `@` and no separator — notably the `admin` of
 * password login — simply come back capitalised (`"Admin"`).
 *
 * The rest of each word is LOWERCASED, not left alone: some tenants store the
 * UPN in caps, and `"MIHAI.POPESCU@..."` must not render as shouting in the nav.
 * The cost is that intercaps get flattened (`McDonald` -> `Mcdonald`), which is
 * the better trade for a directory whose addresses are conventionally lowercase.
 */
export function displayName(identity: string): string {
  const local = identity.split("@")[0];
  if (!local) return identity;
  return local
    .split(".")
    .filter(Boolean)
    .map((word) =>
      word
        .split("-")
        .map((part) =>
          part ? part.charAt(0).toUpperCase() + part.slice(1).toLowerCase() : part
        )
        .join("-")
    )
    .join(" ");
}

/**
 * Compact timestamp for lists and feeds: "13:46:55" for today, "26 Jul, 13:46"
 * for anything older.
 *
 * Backend timestamps are UTC; these render in the VIEWER's local timezone (the
 * browser's) rather than UTC, so a user in Europe/Bucharest sees local wall
 * time. Using the toLocale* family (not toISOString, which forces UTC) keeps
 * this correct for any viewer's timezone, not just one hardcoded offset.
 *
 * "Today" is decided on LOCAL calendar fields (getFullYear/getMonth/getDate),
 * matching the timezone the time itself is rendered in — comparing UTC dates
 * would flip the branch a few hours early or late for viewers off UTC.
 *
 * Seconds are dropped on the older branch on purpose: at that distance the
 * exact second no longer helps, and the day + time is what disambiguates.
 * Anyone needing the full value hovers for :func:`formatTimestampFull`.
 */
/**
 * Rewrite `27 Jul 2026 16:26:21 UTC` inside LLM-generated prose into the viewer's
 * local zone.
 *
 * Two paths produce timestamps in a chat answer. A SCOPED question sends the
 * browser's zone, so the backend already formats that context locally and the
 * model quotes local time. But records from the INDEX carry UTC — they are shared
 * by every viewer, so they cannot be pre-formatted per reader. This closes that
 * second path.
 *
 * Deliberately conservative: it matches only the exact shape `human_time` emits.
 * If the model reformats or paraphrases a timestamp the pattern misses it and the
 * UTC value shows through — mildly unhelpful, never WRONG, which is the right
 * failure direction for a display transform on generated text.
 *
 * The trailing " UTC" is dropped once converted: keeping it would label a local
 * time as UTC, which is worse than no label.
 */
const UTC_STAMP_RE =
  /\b(\d{1,2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (\d{4}) (\d{2}):(\d{2}):(\d{2}) UTC\b/g;

export function localizeUtcStamps(text: string): string {
  if (!text.includes(" UTC")) return text; // fast path — most answers have none
  return text.replace(
    UTC_STAMP_RE,
    (whole, day, mon, year, hh, mm, ss) => {
      const iso = `${year}-${String(MONTHS.indexOf(mon) + 1).padStart(2, "0")}-${String(
        day
      ).padStart(2, "0")}T${hh}:${mm}:${ss}Z`;
      const d = new Date(iso);
      if (Number.isNaN(d.getTime())) return whole; // unparseable: leave it alone
      return d.toLocaleString([], {
        day: "2-digit",
        month: "short",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      });
    }
  );
}

const MONTHS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

export function formatTime(iso: string): string {
  const d = new Date(iso);
  const now = new Date();
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  if (sameDay) {
    return d.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  }
  // Older than today → show the day, so entries across days are distinguishable
  // (two rows an exact 24h apart used to render identically).
  return d.toLocaleString([], {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

/**
 * Full absolute timestamp for hover tooltips (`title` attributes) — the precise
 * value behind the compact one `formatTime` renders. Always includes the year
 * and seconds, so nothing is guessed from an abbreviated display.
 */
export function formatTimestampFull(iso: string): string {
  return new Date(iso).toLocaleString([], {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

// Display-only: capitalizes the first letter for rendering. Never apply this
// to a value used for matching/routing/lookup (e.g. Department, BadgeStatus,
// department-to-Teams-channel keys) - those must stay exactly as the backend
// contract defines them.
export function capitalize(value: string): string {
  return value.length === 0 ? value : value.charAt(0).toUpperCase() + value.slice(1);
}

/**
 * Display-only: turn a backend bucket key into a readable chart label —
 * "INBOUND_TRANSFORM_FAILED" -> "Inbound transform failed", "unassigned" ->
 * "Unassigned". Never use the result for matching or lookup; the raw key stays
 * the identity (charts keep it for the tooltip).
 */
export function humanizeKey(key: string): string {
  return capitalize(key.replace(/_/g, " ").toLowerCase());
}

/**
 * Render a duration in seconds compactly: "820ms" / "12.3s" / "1m 20s".
 *
 * `null` (no finished journey had both timestamps) renders as an em dash rather
 * than "0s" — those are different facts and must not look the same.
 */
export function formatDuration(seconds: number | null): string {
  if (seconds === null) return "—";
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  // Round the remainder, but never to a bare "60s" — carry it into the minutes.
  const rest = Math.round(seconds - minutes * 60);
  if (rest === 60) return `${minutes + 1}m`;
  return rest === 0 ? `${minutes}m` : `${minutes}m ${rest}s`;
}

/**
 * "Updated just now" / "Updated 3 min ago" / "Updated 14:32" / "Collecting…".
 *
 * The only relative formatter here — everything else in this file renders an
 * absolute instant. It exists for "last updated" labels, where the AGE is the fact
 * worth reading rather than the wall-clock time.
 *
 * **Minute granularity, deliberately.** An earlier version ticked mm:ss every
 * second. Two things were wrong with that: the data behind it only changes on the
 * refresh interval, so a running seconds counter is false precision — it implies
 * the page knows something new every second when nothing has moved; and it made
 * the only permanently animated element on a page built for careful reading.
 * Past an hour it switches to an absolute time: at that distance "Updated 14:32"
 * is both shorter and more useful than a minute count nobody will subtract.
 *
 * `now` is a parameter rather than a `Date.now()` call inside so tests get a fixed
 * frame of reference, and so the caller controls when the value is recomputed.
 *
 * A negative age clamps to "just now" rather than rendering a future time: the
 * server stamps the timestamp and the browser subtracts it, so a second or two of
 * clock skew between the two machines is normal and must not produce garbage.
 */
export function formatUpdatedAt(iso: string | null, now: number): string {
  // Not "Updated 0 min ago" — nothing has been collected, so there is no
  // "updated" to report. This is the cold-start state, and saying it plainly is
  // the whole reason the timestamp is nullable.
  if (iso === null) return "Collecting…";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "Collecting…";

  const seconds = Math.max(0, Math.floor((now - then) / 1000));
  if (seconds < 60) return "Updated just now";

  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `Updated ${minutes} min ago`;

  return `Updated ${new Date(then).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  })}`;
}

export function stoppedAt(journey: Journey): string {
  const events = journey.events ?? [];
  const lastEvent = events[events.length - 1];
  return lastEvent ? lastEvent.raw.app_name : "—";
}
