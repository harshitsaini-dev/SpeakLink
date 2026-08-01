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
