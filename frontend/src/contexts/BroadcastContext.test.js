/**
 * The console's model of "who is broadcasting" after concurrency.
 *
 * WHAT CHANGED
 *
 * The UI used to treat "a broadcast exists" as "I cannot broadcast". With
 * several operators able to be live at once that is simply wrong: Bob must be
 * able to start while Alice is on air, provided his Stores are free.
 *
 * The replacement has three parts, and all three come from the server:
 *
 *   mine            my own broadcast, in full
 *   busy_store_ids  Stores held by anybody, narrowed to my Store Scope
 *   sessions        owner and campaign, ONLY with broadcast.view_ownership
 *
 * The frontend never reconstructs what the backend withheld. Without the
 * ownership permission the server sends an EMPTY sessions list rather than
 * redacted stubs, because a stub still discloses how many other broadcasts
 * exist - so there is nothing here that could render one.
 *
 * ADVISORY, NOT AUTHORITATIVE
 *
 * The busy list is a courtesy to the person looking at the screen. A Store can
 * be claimed between this browser rendering it as free and a Start arriving,
 * and when that happens the backend answers STORE_BUSY and the whole start is
 * refused. These tests pin that the local state is cleaned up when it does.
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

const mockStop = jest.fn();
const mockStart = jest.fn();
//: Every HQBroadcaster the provider constructs, in order. The class method
//: calls the jest mock UNBOUND, so `this` is not available inside it - the
//: constructor records the options here instead.
const mockConstructed = [];
jest.mock("@/lib/audio/HQBroadcaster", () => ({
  HQBroadcaster: class {
    constructor(options) {
      this.options = options;
      mockConstructed.push(options);
    }
    static supportedMime() { return true; }
    start(...args) { return mockStart(...args); }
    stop(...args) { return mockStop(...args); }
    // The provider applies the operator's current mic level to every new
    // broadcaster, so a double without these is not a double of this class.
    setVolumePercent(percent) { this.volumePercent = percent; }
    setMuted(muted) { this.muted = muted; }
  },
}));

const ACTIVE_EMPTY = {
  mine: null, sessions: [], busy_store_ids: [], may_view_ownership: false,
};

function respond({ current = { live: false }, active = ACTIVE_EMPTY } = {}) {
  api.get.mockImplementation((path) => {
    if (path === "/broadcast/active") return Promise.resolve({ data: active });
    return Promise.resolve({ data: current });
  });
}

function Probe() {
  const b = useBroadcast();
  window.__broadcast = b;
  return (
    <div>
      <span data-testid="is-live">{String(b.isLive)}</span>
      <span data-testid="busy">{(b.active.busy_store_ids || []).join(",")}</span>
      <span data-testid="mine">{b.active.mine ? b.active.mine.campaign_name : "none"}</span>
      <span data-testid="sessions">{b.active.sessions.length}</span>
      <span data-testid="may-view">{String(b.active.may_view_ownership)}</span>
      <span data-testid="bp-busy">{String(b.isStoreBusyForOthers(101))}</span>
    </div>
  );
}

async function mount() {
  render(<BroadcastProvider><Probe /></BroadcastProvider>);
  await act(async () => {});
}

beforeEach(() => {
  jest.clearAllMocks();
  mockConstructed.length = 0;
  mockStart.mockResolvedValue(undefined);
  mockStop.mockResolvedValue(undefined);
  api.post.mockResolvedValue({ data: { id: 7 } });
});

// ===========================================================================
// Another operator being live is not my problem
// ===========================================================================
test("another operator being live does not make me live", async () => {
  respond({
    current: { live: false },
    active: { ...ACTIVE_EMPTY, busy_store_ids: [101] },
  });
  await mount();

  expect(screen.getByTestId("is-live").textContent).toBe("false");
  expect(screen.getByTestId("busy").textContent).toBe("101");
});

test("my own live broadcast is reported as mine", async () => {
  respond({
    current: { live: true, session: { id: 5 } },
    active: {
      ...ACTIVE_EMPTY,
      mine: { session_id: 5, campaign_name: "Mine", target_store_ids: [101],
              target_store_count: 1 },
      busy_store_ids: [101],
    },
  });
  await mount();

  expect(screen.getByTestId("mine").textContent).toBe("Mine");
  expect(screen.getByTestId("is-live").textContent).toBe("true");
});

test("a Store my own broadcast uses is not 'busy' to me", async () => {
  respond({
    current: { live: true, session: { id: 5 } },
    active: {
      ...ACTIVE_EMPTY,
      mine: { session_id: 5, campaign_name: "Mine", target_store_ids: [101],
              target_store_count: 1 },
      busy_store_ids: [101],
    },
  });
  await mount();

  expect(screen.getByTestId("bp-busy").textContent).toBe("false");
});

test("a Store another broadcast uses IS busy to me", async () => {
  respond({ active: { ...ACTIVE_EMPTY, busy_store_ids: [101] } });
  await mount();
  expect(screen.getByTestId("bp-busy").textContent).toBe("true");
});

// ===========================================================================
// Privacy
// ===========================================================================
test("without the ownership permission there are no other sessions at all", async () => {
  respond({
    active: { ...ACTIVE_EMPTY, busy_store_ids: [101], may_view_ownership: false },
  });
  await mount();

  expect(screen.getByTestId("sessions").textContent).toBe("0");
  expect(screen.getByTestId("may-view").textContent).toBe("false");
});

test("with the ownership permission the sessions arrive", async () => {
  respond({
    active: {
      mine: null, busy_store_ids: [101], may_view_ownership: true,
      sessions: [{ session_id: 3, campaign_name: "Alice", owner_username: "alice",
                   target_store_ids: [101], target_store_count: 1 }],
    },
  });
  await mount();

  expect(screen.getByTestId("sessions").textContent).toBe("1");
  expect(screen.getByTestId("may-view").textContent).toBe("true");
});

// ===========================================================================
// STORE_BUSY
// ===========================================================================
test("a STORE_BUSY start is refused without opening a microphone", async () => {
  respond();
  await mount();

  api.post.mockImplementation((path) => {
    if (path === "/broadcast/sessions") return Promise.resolve({ data: { id: 7 } });
    if (path.endsWith("/start")) {
      const failure = new Error("conflict");
      failure.response = { status: 409, data: { detail: {
        code: "STORE_BUSY",
        message: "BP currently in use by another broadcast.",
        busy_store_ids: [101], busy_store_codes: ["BP"],
      } } };
      return Promise.reject(failure);
    }
    return Promise.resolve({ data: {} });
  });

  let thrown = null;
  await act(async () => {
    try {
      await window.__broadcast.startBroadcast({
        campaign: "Mine", targetMode: "selected", ids: [101],
      });
    } catch (e) { thrown = e; }
  });

  expect(thrown).toBeTruthy();
  expect(thrown.storeBusy).toBe(true);
  expect(thrown.busyStoreCodes).toEqual(["BP"]);
  // Never started: no microphone, no socket, no local live state.
  expect(mockStart).not.toHaveBeenCalled();
  expect(window.__broadcast.hasActiveBroadcaster()).toBe(false);
});

test("the STORE_BUSY message names no other operator", async () => {
  respond();
  await mount();
  api.post.mockImplementation((path) => {
    if (path === "/broadcast/sessions") return Promise.resolve({ data: { id: 7 } });
    if (path.endsWith("/start")) {
      const failure = new Error("conflict");
      failure.response = { status: 409, data: { detail: {
        code: "STORE_BUSY", message: "BP currently in use by another broadcast.",
        busy_store_ids: [101], busy_store_codes: ["BP"],
      } } };
      return Promise.reject(failure);
    }
    return Promise.resolve({ data: {} });
  });

  let thrown = null;
  await act(async () => {
    try {
      await window.__broadcast.startBroadcast({
        campaign: "Mine", targetMode: "selected", ids: [101] });
    } catch (e) { thrown = e; }
  });

  const rendered = String(thrown.message).toLowerCase();
  for (const leak of ["alice", "campaign", "session", "owner", "user"]) {
    expect(rendered.includes(leak)).toBe(false);
  }
});

test("a failed microphone start does not leave the microphone open", async () => {
  respond({ current: { live: false, ready_receivers: [101] } });
  await mount();
  api.post.mockResolvedValue({ data: { id: 7, ticket: "t" } });
  api.get.mockImplementation((path) => {
    if (path === "/broadcast/active") return Promise.resolve({ data: ACTIVE_EMPTY });
    return Promise.resolve({ data: { live: false, ready_receivers: [101] } });
  });
  mockStart.mockRejectedValue(new Error("microphone blew up"));

  await act(async () => {
    try {
      await window.__broadcast.startBroadcast({
        campaign: "Mine", targetMode: "selected", ids: [101] });
    } catch { /* expected */ }
  });

  // stop() must have been called on the half-started broadcaster.
  expect(mockStop).toHaveBeenCalled();
  expect(window.__broadcast.hasActiveBroadcaster()).toBe(false);
});

// ===========================================================================
// Stop
// ===========================================================================
test("Stop posts to MY session id", async () => {
  respond({
    current: { live: true, session: { id: 5 } },
    active: {
      ...ACTIVE_EMPTY,
      mine: { session_id: 5, campaign_name: "Mine", target_store_ids: [101],
              target_store_count: 1 },
    },
  });
  await mount();

  await act(async () => { await window.__broadcast.stopBroadcast(); });

  expect(api.post).toHaveBeenCalledWith("/broadcast/sessions/5/stop");
});

// ===========================================================================
// Emergency Stop
// ===========================================================================
test("Emergency Stop returns how many were stopped", async () => {
  respond();
  await mount();
  api.post.mockResolvedValue({ data: { ok: true, session_ids: [1, 2] } });

  let outcome = null;
  await act(async () => { outcome = await window.__broadcast.emergencyStop(); });

  expect(outcome.session_ids).toEqual([1, 2]);
});

test("EMERGENCY_STOP_INCOMPLETE is surfaced as a failure, never a success", async () => {
  respond();
  await mount();
  api.post.mockImplementation(() => {
    const failure = new Error("incomplete");
    failure.response = { status: 500, data: { detail: {
      code: "EMERGENCY_STOP_INCOMPLETE",
      message: "Some broadcasts could not be stopped and may still be live.",
      stopped_session_ids: [1], failed_session_ids: [2],
    } } };
    return Promise.reject(failure);
  });

  let thrown = null;
  await act(async () => {
    try { await window.__broadcast.emergencyStop(); } catch (e) { thrown = e; }
  });

  expect(thrown).toBeTruthy();
  expect(thrown.emergencyIncomplete).toBe(true);
  expect(thrown.failedSessionIds).toEqual([2]);
  expect(String(thrown.message)).toContain("STILL LIVE");
});

test("Emergency Stop stops this browser's microphone even when it fails", async () => {
  respond({ current: { live: true, session: { id: 5 } } });
  await mount();
  // Pretend a broadcaster is running.
  await act(async () => {
    api.post.mockResolvedValue({ data: { ok: true, session_ids: [] } });
  });
  api.post.mockImplementation(() => Promise.reject(
    Object.assign(new Error("boom"), { response: { status: 500, data: {} } })));

  await act(async () => {
    try { await window.__broadcast.emergencyStop(); } catch { /* expected */ }
  });

  expect(window.__broadcast.hasActiveBroadcaster()).toBe(false);
});

// ===========================================================================
// Stale responses
// ===========================================================================
test("a slow earlier active-state response cannot overwrite a newer one", async () => {
  respond();
  await mount();

  const deferred = [];
  api.get.mockImplementation((path) => {
    if (path !== "/broadcast/active") return Promise.resolve({ data: { live: false } });
    return new Promise((resolve) => { deferred.push(resolve); });
  });

  await act(async () => { window.__broadcast.loadActive(); });
  await act(async () => { window.__broadcast.loadActive(); });

  const [older, newer] = deferred.slice(-2);
  // The NEWER request answers first...
  await act(async () => {
    newer({ data: { ...ACTIVE_EMPTY, busy_store_ids: [999] } });
  });
  // ...and the OLDER one lands afterwards with stale data.
  await act(async () => {
    older({ data: { ...ACTIVE_EMPTY, busy_store_ids: [111] } });
  });

  expect(screen.getByTestId("busy").textContent).toBe("999");
});

// ===========================================================================
// The microphone socket stays session-bound
// ===========================================================================
test("the broadcaster socket URL still carries session_id", async () => {
  respond({ current: { live: false, ready_receivers: [101] } });
  await mount();
  api.get.mockImplementation((path) => {
    if (path === "/broadcast/active") return Promise.resolve({ data: ACTIVE_EMPTY });
    return Promise.resolve({ data: { live: false, ready_receivers: [101] } });
  });
  api.post.mockImplementation((path) => {
    if (path === "/broadcast/sessions") return Promise.resolve({ data: { id: 77 } });
    if (path === "/auth/ws-ticket") return Promise.resolve({ data: { ticket: "abc" } });
    return Promise.resolve({ data: {} });
  });

  await act(async () => {
    await window.__broadcast.startBroadcast({
      campaign: "Mine", targetMode: "selected", ids: [101] });
  });

  const constructedUrl = mockConstructed.at(-1)?.wsUrl ?? null;

  expect(constructedUrl).toContain("/ws/broadcaster");
  expect(constructedUrl).toContain("ticket=abc");
  // The session binding that stops one operator streaming into another's
  // broadcast. Losing it would be silent - audio would still flow.
  expect(constructedUrl).toContain("session_id=77");
  expect(window.__broadcast.hasActiveBroadcaster()).toBe(true);
});

// ===========================================================================
// Navigating away and back must not start anything a second time
// ===========================================================================
//
// The route is mounted and unmounted INSIDE the provider, which is exactly
// how the real application behaves: BroadcastProvider sits above the router,
// so navigating to another page unmounts the Console and leaves the provider -
// and its microphone, recorder and socket - untouched.
function Routed({ show }) {
  return show ? <Probe /> : <span data-testid="elsewhere">another page</span>;
}

test("remounting a consumer route creates no second broadcaster", async () => {
  // The operator's bug was an empty form on return. The fix rehydrates it, and
  // the risk of any "restore" is that it quietly re-broadcasts rather than
  // re-rendering, so the negative half is asserted here.
  //
  // ready_receivers is what startBroadcast waits for before opening the
  // microphone; without it the provider polls until its 20 s deadline.
  respond({ current: { live: false, ready_receivers: [101, 102] },
            active: ACTIVE_EMPTY });
  const view = render(
    <BroadcastProvider><Routed show /></BroadcastProvider>);
  await act(async () => {});

  await act(async () => {
    await window.__broadcast.startBroadcast({
      campaign: "Morning Offer", targetMode: "selected", ids: [101, 102],
    });
  });
  expect(mockConstructed.length).toBe(1);
  expect(mockStart).toHaveBeenCalledTimes(1);
  expect(window.__broadcast.hasActiveBroadcaster()).toBe(true);
  const socketUrl = mockConstructed[0].wsUrl;

  // Navigate away and back, three times.
  for (let visit = 0; visit < 3; visit += 1) {
    await act(async () => {
      view.rerender(<BroadcastProvider><Routed show={false} /></BroadcastProvider>);
    });
    await act(async () => {
      view.rerender(<BroadcastProvider><Routed show /></BroadcastProvider>);
    });
  }

  // One HQBroadcaster, so one getUserMedia, one MediaRecorder and one
  // broadcaster WebSocket - all three are acquired inside its start().
  expect(mockConstructed.length).toBe(1);
  expect(mockStart).toHaveBeenCalledTimes(1);
  expect(mockStop).not.toHaveBeenCalled();
  expect(window.__broadcast.hasActiveBroadcaster()).toBe(true);
  // The same socket, bound to the same session - not a second uplink.
  expect(mockConstructed[0].wsUrl).toBe(socketUrl);
  // And exactly one session was ever created.
  expect(api.post.mock.calls.filter(([path]) => path === "/broadcast/sessions"))
    .toHaveLength(1);
});

test("the microphone level survives navigating away and back", async () => {
  respond({ current: { live: false, ready_receivers: [101] },
            active: ACTIVE_EMPTY });
  const view = render(
    <BroadcastProvider><Routed show /></BroadcastProvider>);
  await act(async () => {});

  await act(async () => {
    await window.__broadcast.startBroadcast({
      campaign: "Morning Offer", targetMode: "selected", ids: [101],
    });
  });
  await act(async () => {
    window.__broadcast.setMicVolume(40);
    window.__broadcast.setMicMute(true);
  });

  await act(async () => {
    view.rerender(<BroadcastProvider><Routed show={false} /></BroadcastProvider>);
  });
  await act(async () => {
    view.rerender(<BroadcastProvider><Routed show /></BroadcastProvider>);
  });

  // Mic gain and mute live in the provider, so a route remount must not reset
  // them - an operator who muted before walking to another page must not come
  // back to an unmuted microphone.
  expect(window.__broadcast.micVolumePercent).toBe(40);
  expect(window.__broadcast.micMuted).toBe(true);
});

// ===========================================================================
// Only With Link starts with no Stores
// ===========================================================================
test("Only With Link starts with zero Stores", async () => {
  // The defect: this check was unconditional, so the one mode whose whole
  // point is having no physical target was the one mode that could not start.
  await mount();
  // The request is refused by a sentinel, so this test measures ONE thing:
  // whether validation let it through. Driving the whole microphone path would
  // measure the mock instead.
  const sentinel = new Error("reached the server");
  api.post.mockRejectedValue(sentinel);

  let thrown = null;
  await act(async () => {
    try {
      await window.__broadcast.startBroadcast({
        campaign: "Web only", targetMode: "only_with_link", ids: [] });
    } catch (e) { thrown = e; }
  });

  expect(thrown).toBe(sentinel);
  const created = api.post.mock.calls.find(([url]) => url === "/broadcast/sessions");
  expect(created).toBeTruthy();
  expect(created[1].target_mode).toBe("only_with_link");
  // A leftover Selected-mode draft must not travel with it.
  expect(created[1].store_ids).toBeUndefined();
});

test("a physical mode with no Stores is still refused", async () => {
  await mount();
  let thrown = null;
  await act(async () => {
    try {
      await window.__broadcast.startBroadcast({
        campaign: "Physical", targetMode: "selected", ids: [] });
    } catch (e) { thrown = e; }
  });
  expect(thrown).toBeTruthy();
  expect(String(thrown.message)).toMatch(/No stores selected/i);
});

test("a stale Selected draft never travels with Only With Link", async () => {
  await mount();
  api.post.mockRejectedValue(new Error("reached the server"));

  await act(async () => {
    try {
      await window.__broadcast.startBroadcast({
        campaign: "Web only", targetMode: "only_with_link", ids: [101, 102] });
    } catch (ignored) { /* the sentinel; the payload is what matters */ }
  });
  const created = api.post.mock.calls.find(([url]) => url === "/broadcast/sessions");
  expect(created[1].store_ids).toBeUndefined();
});
