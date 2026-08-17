/**
 * Going live, and the twenty seconds nobody could see into.
 *
 * REPORTED: the Confirm dialog sat on "Starting…" for a long time, and then
 * said no Receiver reported READY. Both halves were real, and neither was
 * what the operator thought.
 *
 * The wait is a poll of /broadcast/current with a twenty-second deadline. But
 * the deadline is only checked BETWEEN requests, and the axios client has no
 * timeout - so a request that stalls rather than fails is waited on forever
 * and the gate never expires. That is the indefinite "Starting…".
 *
 * And the refusal blamed the Receiver and FFmpeg. In the case that produced
 * the report the Store's socket had died (WinError 121 in the HQ log) and it
 * reconnected minutes later - the Receiver was running the whole time and
 * FFmpeg had nothing to do with it. A message that sends somebody to the
 * wrong machine costs more than one that says less.
 */
import React from "react";
import { render, screen, act } from "@testing-library/react";
import { BroadcastProvider, useBroadcast } from "./BroadcastContext";
import { api } from "@/lib/api";

jest.mock("@/lib/api", () => ({
  api: { get: jest.fn(), post: jest.fn() },
  getToken: () => "test-token",
  wsUrl: (path) => `ws://localhost:8000${path}`,
}));

jest.mock("@/lib/audio/HQBroadcaster", () => ({
  HQBroadcaster: class {
    constructor(options) { this.options = options; }
    static supportedMime() { return true; }
    start() { return Promise.resolve(); }
    stop() { return Promise.resolve(); }
    setVolumePercent() {}
    setMuted() {}
  },
}));

const ACTIVE_EMPTY = {
  mine: null, sessions: [], busy_store_ids: [], may_view_ownership: false,
};

/** The readiness gate polls /broadcast/current; everything else is scenery. */
function respondCurrent(handler) {
  api.get.mockImplementation((path, options) => {
    if (path === "/broadcast/active") {
      return Promise.resolve({ data: ACTIVE_EMPTY });
    }
    return handler(options);
  });
}

let started;
function Probe() {
  const { startBroadcast } = useBroadcast();
  started = startBroadcast;
  return <span data-testid="ready" />;
}

const GO_LIVE = { campaign: "Sale", targetMode: "selected", ids: [31],
                  region: "", city: "" };

// FAKE TIME, because the gate is twenty seconds long and three tests of it
// would put a real minute into a suite that runs in twenty-five seconds. The
// clock is faked along with the timers on purpose: the loop's deadline is
// Date.now()-based, so a fake setTimeout with a real clock would spin.
async function goLive() {
  render(<BroadcastProvider><Probe /></BroadcastProvider>);
  await act(async () => {});
  await screen.findByTestId("ready");

  let refusal = null;
  let settled = false;
  jest.useFakeTimers();
  try {
    await act(async () => {
      const attempt = started(GO_LIVE)
        .catch((failure) => { refusal = failure; })
        .finally(() => { settled = true; });
      // Advanced in steps rather than one jump: each poll's promise has to be
      // allowed to resolve before the next timer fires. The microtask flush is
      // explicit because this Jest has no advanceTimersByTimeAsync - moving
      // the clock alone would leave every poll's promise unsettled.
      for (let elapsed = 0; elapsed <= 25000 && !settled; elapsed += 400) {
        jest.advanceTimersByTime(400);
        // eslint-disable-next-line no-await-in-loop
        for (let flush = 0; flush < 5; flush += 1) await Promise.resolve();
      }
      await attempt;
    });
  } finally {
    jest.useRealTimers();
  }
  return refusal;
}

beforeEach(() => {
  jest.clearAllMocks();
  api.post.mockResolvedValue({ data: { id: 7 } });
});

test("every readiness poll asks for a timeout", async () => {
  // Without one, a request that stalls instead of failing is waited on
  // forever: the loop only re-checks its deadline between requests. This is
  // the whole of the indefinite "Starting…".
  const seen = [];
  respondCurrent((options) => {
    seen.push(options);
    return Promise.resolve({
      data: { live: true, online_receivers: [31], ready_receivers: [] } });
  });

  await goLive();

  const polls = seen.filter(Boolean);
  expect(polls.length).toBeGreaterThan(0);
  polls.forEach((options) => expect(options.timeout).toBeGreaterThan(0));
});

test("a Store that stayed connected is reported as an unacknowledged Receiver", async () => {
  respondCurrent(() => Promise.resolve({
    data: { live: true, online_receivers: [31], ready_receivers: [] } }));

  const refusal = await goLive();
  expect(refusal.message).toMatch(/connected but did not acknowledge/i);
  expect(refusal.message).toMatch(/FFmpeg/);
});

test("a Store whose link dropped is not blamed on its Receiver", async () => {
  // Connected on the first answer, gone from the online list afterwards -
  // the shape of a socket dying mid-wait.
  let answered = 0;
  respondCurrent(() => {
    answered += 1;
    return Promise.resolve({
      data: {
        live: true,
        online_receivers: answered === 1 ? [31] : [],
        ready_receivers: [],
      },
    });
  });

  const refusal = await goLive();
  expect(refusal.message).toMatch(/connection dropped/i);
  expect(refusal.message).toMatch(/network link/i);
  // The half that matters: it must NOT send this operator to look at FFmpeg
  // on a machine that was never the problem.
  expect(refusal.message).not.toMatch(/FFmpeg/);
});
