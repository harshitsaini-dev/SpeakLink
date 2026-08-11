/**
 * Regression guard for the broadcast-timer defect.
 *
 * Real evidence: the first live broadcast showed ~05:30:28 elapsed
 * immediately after "Start Live Broadcast" instead of ~00:00:00. That is
 * exactly the UTC+05:30 (IST) offset of the browser that observed it, and it
 * happened because a naive (offset-less) timestamp string was parsed by
 * `new Date(...)` as local time instead of UTC.
 */
import { parseUtcMs, elapsedSeconds, formatIst } from "./time";

describe("parseUtcMs", () => {
  test("a Z-suffixed UTC string parses to the correct epoch regardless of local zone", () => {
    expect(parseUtcMs("2026-08-01T05:30:00.000Z")).toBe(Date.UTC(2026, 7, 1, 5, 30, 0, 0));
  });

  test("an explicit +00:00 offset is honoured", () => {
    expect(parseUtcMs("2026-08-01T05:30:00+00:00")).toBe(Date.UTC(2026, 7, 1, 5, 30, 0, 0));
  });

  test("a string with NO offset is still treated as UTC, not local time", () => {
    // This is the defensive net: even if a naive value slipped through the
    // backend contract, the frontend must not silently reinterpret it as
    // local time the way `new Date("2026-08-01T05:30:00")` would.
    expect(parseUtcMs("2026-08-01T05:30:00")).toBe(Date.UTC(2026, 7, 1, 5, 30, 0, 0));
  });

  test("null/empty input returns null rather than NaN", () => {
    expect(parseUtcMs(null)).toBeNull();
    expect(parseUtcMs("")).toBeNull();
    expect(parseUtcMs(undefined)).toBeNull();
  });
});

describe("elapsedSeconds - the exact regression this defect requires", () => {
  test("a session that started 1 second ago shows ~1s elapsed, not ~19801s (05:30:01)", () => {
    const startedAt = "2026-08-01T05:30:00.000Z";
    const oneSecondLater = Date.UTC(2026, 7, 1, 5, 30, 1, 0);

    const elapsed = elapsedSeconds(startedAt, oneSecondLater);

    expect(elapsed).toBe(1);
    // 5 hours 30 minutes = 19800 seconds - the UTC+05:30 regression value.
    expect(elapsed).not.toBe(19800 + 1);
  });

  test("immediately after start, elapsed is 0 or 1, never ~05:30:xx worth of seconds", () => {
    const startedAt = "2026-08-01T12:00:00.000Z";
    const rightAfter = Date.UTC(2026, 7, 1, 12, 0, 1, 0);

    const elapsed = elapsedSeconds(startedAt, rightAfter);

    expect(elapsed).toBeLessThanOrEqual(1);
  });

  test("after roughly 10 seconds it reports roughly 10, from epoch values only", () => {
    const startedAt = "2026-08-01T12:00:00.000Z";
    const tenSecondsLater = Date.UTC(2026, 7, 1, 12, 0, 10, 0);

    expect(elapsedSeconds(startedAt, tenSecondsLater)).toBe(10);
  });

  test("a naive (offset-less) started_at still yields a small elapsed value, never +5:30", () => {
    const naiveStartedAt = "2026-08-01T12:00:00.123456"; // no Z, no offset
    const rightAfterUtc = Date.UTC(2026, 7, 1, 12, 0, 1, 0);

    const elapsed = elapsedSeconds(naiveStartedAt, rightAfterUtc);

    expect(elapsed).toBeLessThanOrEqual(1);
    expect(elapsed).not.toBeGreaterThan(19800);
  });
});

describe("formatIst - known UTC instant, expected Asia/Kolkata display", () => {
  test("2026-08-01T12:00:00Z (noon UTC) displays as 5:30 PM IST", () => {
    const display = formatIst("2026-08-01T12:00:00.000Z", {
      year: undefined, month: undefined, day: undefined,
      hour: "2-digit", minute: "2-digit", hour12: true,
    });
    expect(display.replace(/\s+/g, " ")).toMatch(/5:30:00\s*PM/i);
  });

  test("a missing value renders as an em dash rather than 'Invalid Date'", () => {
    expect(formatIst(null)).toBe("—");
    expect(formatIst(undefined)).toBe("—");
  });
});
