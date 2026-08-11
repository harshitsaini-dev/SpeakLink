/**
 * Timezone-safe timestamp handling for the HQ dashboard.
 *
 * The backend always emits UTC ISO-8601 with an explicit "Z" (see
 * backend/schemas.py `_utc_iso`). This module is the single place that turns
 * such a string into an epoch and into a human-readable Asia/Kolkata clock
 * time - so nothing else in the app calls `new Date(iso).getTime()` directly
 * and risks re-introducing the UTC+05:30 defect if a future field ever ships
 * a naive value again.
 */

const HAS_EXPLICIT_TZ = /Z$|[+-]\d{2}:?\d{2}$/;

/**
 * Epoch milliseconds for a backend timestamp, treating an offset-less string
 * as UTC rather than letting the browser guess local time.
 *
 * This is deliberately defensive: the backend contract is "always carries a
 * Z", but a client-side safety net that assumes UTC on a missing offset can
 * never produce the UTC+05:30 defect, whereas trusting `new Date(...)`
 * directly can.
 */
export function parseUtcMs(iso) {
  if (!iso) return null;
  const normalized = HAS_EXPLICIT_TZ.test(iso) ? iso : `${iso}Z`;
  const ms = Date.parse(normalized);
  return Number.isNaN(ms) ? null : ms;
}

export const IST_TIME_ZONE = "Asia/Kolkata";

/** Human-readable absolute timestamp, always in Asia/Kolkata for this deployment. */
export function formatIst(iso, options = {}) {
  const ms = parseUtcMs(iso);
  if (ms === null) return "—";
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: IST_TIME_ZONE,
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: true,
    ...options,
  }).format(new Date(ms));
}

/**
 * Elapsed seconds between a UTC start timestamp and now, from epoch values -
 * never from a formatted local clock string, so it cannot drift with the
 * viewer's timezone.
 */
export function elapsedSeconds(startIso, nowMs = Date.now()) {
  const startMs = parseUtcMs(startIso);
  if (startMs === null) return 0;
  return Math.max(0, Math.floor((nowMs - startMs) / 1000));
}

/**
 * The header clock: "11-August-2026 07:23:21 PM IST".
 *
 * Written out with the month in full and the zone named, because this is the
 * timestamp an operator quotes when they report what a Broadcast did - and a
 * bare "07:23" in a screenshot is unfalsifiable a week later. Always
 * Asia/Kolkata regardless of what the viewing machine is set to, so two people
 * comparing notes are comparing the same clock.
 */
export function formatIstClock(dateMs = Date.now()) {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: IST_TIME_ZONE,
    day: "2-digit", month: "long", year: "numeric",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: true,
  }).formatToParts(new Date(dateMs)).reduce((all, part) => {
    all[part.type] = part.value;
    return all;
  }, {});
  const meridiem = (parts.dayPeriod || "").toUpperCase();
  return `${parts.day}-${parts.month}-${parts.year} ${parts.hour}:${parts.minute}:${parts.second} ${meridiem} IST`;
}

/** Just the clock time of a backend timestamp: "07:23 PM", Asia/Kolkata. */
export function formatIstTimeOfDay(iso) {
  const ms = parseUtcMs(iso);
  if (ms === null) return "";
  return new Intl.DateTimeFormat("en-US", {
    timeZone: IST_TIME_ZONE,
    hour: "2-digit", minute: "2-digit", hour12: true,
  }).format(new Date(ms));
}
