/**
 * Returning to Broadcast Console while a broadcast is still on air.
 *
 * The operator's report: start a broadcast, navigate to another page, come
 * back, and the Console shows an empty Campaign Name with no Stores selected -
 * while the broadcast is still running and still reaching those Stores.
 *
 * The cause is that campaign, targetMode and selectedIds are component-local
 * React state and die with the route, whereas the broadcast lives in
 * BroadcastProvider above the router and does not.
 *
 * These tests unmount and remount the component rather than asserting on a
 * restore helper, because unmounting is precisely what breaks it. They also
 * assert the negative half - that nothing starts a second broadcast - since a
 * "restore" that quietly re-broadcasts would satisfy the positive assertions.
 */
import React from "react";
import { render, screen, act, cleanup } from "@testing-library/react";
import BroadcastConsole from "./BroadcastConsole";

const STORES = [
  { id: 101, store_code: "BP", store_name: "Testville North", city: "DELHI",
    region: "NORTH", is_online_store: false, status: "online" },
  { id: 102, store_code: "RG", store_name: "Testville South", city: "DELHI",
    region: "NORTH", is_online_store: false, status: "online" },
  { id: 103, store_code: "VP", store_name: "Testville East", city: "CHENNAI",
    region: "SOUTH", is_online_store: false, status: "online" },
];

let mockBroadcast;
let mockPermissions;

jest.mock("react-router-dom", () => ({
  Link: ({ to, children, ...rest }) => <a href={to} {...rest}>{children}</a>,
}), { virtual: true });
jest.mock("@/contexts/BroadcastContext", () => ({
  useBroadcast: () => mockBroadcast,
}));
jest.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ can: (code) => mockPermissions.has(code) }),
}));
jest.mock("@/lib/api", () => ({
  api: { get: jest.fn(), post: jest.fn() },
  getToken: () => "t",
  wsUrl: (p) => `ws://localhost:8000${p}`,
}));

// eslint-disable-next-line import/first
const { api } = require("@/lib/api");

/** The provider's state, which deliberately SURVIVES unmounting the route. */
function providerState({ live = false, session = null, targets = [],
                         mine = null } = {}) {
  return {
    current: live ? { live: true, session, targets } : { live: false },
    load: jest.fn(async () => {}),
    isLive: live,
    meter: 0,
    micLevels: { input: 0, sent: 0 },
    micVolumePercent: 100,
    micMuted: false,
    setMicVolume: jest.fn(),
    setMicMute: jest.fn(),
    micEffectivelySilent: false,
    broadcasterStatus: live ? "recording" : "idle",
    error: "",
    setError: jest.fn(),
    startBroadcast: jest.fn(async () => {}),
    stopBroadcast: jest.fn(async () => {}),
    emergencyStop: jest.fn(async () => ({ ok: true })),
    active: {
      mine, sessions: [], busy_store_ids: [], may_view_ownership: false,
      may_view_targets: false, may_manage_active: false, active_count: null,
    },
    isStoreBusyForOthers: () => false,
  };
}

const LIVE_SESSION = {
  id: 77, campaign_name: "Morning Offer", status: "live",
  target_mode: "selected", started_at: new Date(0).toISOString(),
};
const LIVE_TARGETS = [
  { id: 1, store_id: 101, play_status: "audio_receiving" },
  { id: 2, store_id: 102, play_status: "audio_receiving" },
  { id: 3, store_id: 103, play_status: "audio_receiving" },
];

async function mountConsole() {
  const view = render(<BroadcastConsole />);
  await act(async () => {});
  return view;
}

beforeEach(() => {
  jest.clearAllMocks();
  // Physical broadcaster: broadcast.store_delivery now decides whether the
  // Console asks for the target catalogue at all.
  mockPermissions = new Set(["broadcast.store_delivery", "broadcast.start", "broadcast.stop",
                             "store_audio.control"]);
  mockBroadcast = providerState();
  api.get.mockImplementation((path) => {
    if (path === "/broadcast/target-stores") {
      return Promise.resolve({
        data: { stores: STORES, regions: ["NORTH", "SOUTH"],
                cities: ["DELHI", "CHENNAI"] },
      });
    }
    if (typeof path === "string" && path.includes("/audio-control")) {
      return Promise.resolve({ data: { session_id: 77, stores: [] } });
    }
    return Promise.resolve({ data: {} });
  });
  api.post.mockResolvedValue({ data: { session_id: 77, stores: [] } });
});

afterEach(cleanup);

// ===========================================================================
// Restoration
// ===========================================================================
test("the campaign name is restored after navigating away and back", async () => {
  const { unmount } = await mountConsole();
  unmount();                       // navigating to another page

  mockBroadcast = providerState({
    live: true, session: LIVE_SESSION, targets: LIVE_TARGETS,
    mine: { session_id: 77, campaign_name: "Morning Offer",
            target_store_ids: [101, 102, 103] },
  });
  await mountConsole();            // navigating back

  expect(screen.getByTestId("campaign-name-input").value).toBe("Morning Offer");
});

test("the selected Stores are restored, matched by id", async () => {
  mockBroadcast = providerState({
    live: true, session: LIVE_SESSION, targets: LIVE_TARGETS,
  });
  await mountConsole();

  for (const code of ["BP", "RG", "VP"]) {
    expect(screen.getByTestId(`store-checkbox-${code}`).checked).toBe(true);
  }
});

test("a Store that is NOT a target stays unselected", async () => {
  mockBroadcast = providerState({
    live: true, session: LIVE_SESSION,
    targets: [{ id: 1, store_id: 101, play_status: "audio_receiving" }],
  });
  await mountConsole();

  expect(screen.getByTestId("store-checkbox-BP").checked).toBe(true);
  expect(screen.getByTestId("store-checkbox-RG").checked).toBe(false);
  expect(screen.getByTestId("store-checkbox-VP").checked).toBe(false);
});

test("the target mode is restored from the live session", async () => {
  mockBroadcast = providerState({
    live: true,
    session: { ...LIVE_SESSION, target_mode: "region" },
    targets: LIVE_TARGETS,
  });
  await mountConsole();

  expect(screen.getByTestId("target-mode-select").value).toBe("region");
});

test("a region broadcast still shows its real targets after a remount", async () => {
  // Region and City are not stored on the session, so recomputing the target
  // set from the dropdown would resolve to nothing here. The live session's
  // own target list is used instead.
  mockBroadcast = providerState({
    live: true,
    session: { ...LIVE_SESSION, target_mode: "region" },
    targets: [{ id: 1, store_id: 101, play_status: "audio_receiving" },
              { id: 2, store_id: 102, play_status: "audio_receiving" }],
  });
  await mountConsole();

  expect(screen.getByTestId("target-mode-select").value).toBe("region");
  // Region mode renders no checkbox column, so the proof is the target count:
  // the two Stores the session really reaches. Recomputing from the Region
  // dropdown would have produced ZERO here, because Region is not stored on
  // the session and the dropdown is empty after a remount.
  expect(screen.getByTestId("stat-online").textContent).toContain("2");
});

test("the restored form is read-only while the broadcast is live", async () => {
  mockBroadcast = providerState({
    live: true, session: LIVE_SESSION, targets: LIVE_TARGETS,
  });
  await mountConsole();

  // Existing product behaviour, preserved: the operator must not think
  // clicking a checkbox changes what is on air.
  expect(screen.getByTestId("campaign-name-input").disabled).toBe(true);
  expect(screen.getByTestId("target-mode-select").disabled).toBe(true);
  expect(screen.getByTestId("store-checkbox-BP").disabled).toBe(true);
});

// ===========================================================================
// Nothing is started a second time
// ===========================================================================
test("returning to a live Console starts no second broadcast", async () => {
  mockBroadcast = providerState({
    live: true, session: LIVE_SESSION, targets: LIVE_TARGETS,
  });
  await mountConsole();

  // The provider owns the microphone, the recorder and the socket; the route
  // remounting must not ask it for another one.
  expect(mockBroadcast.startBroadcast).not.toHaveBeenCalled();
  const posted = api.post.mock.calls.map(([path]) => path);
  expect(posted).not.toContain("/broadcast/sessions");
});

test("Store audio controls bind to the SAME live session id", async () => {
  mockBroadcast = providerState({
    live: true, session: LIVE_SESSION, targets: LIVE_TARGETS,
    mine: { session_id: 77, campaign_name: "Morning Offer",
            target_store_ids: [101, 102, 103] },
  });
  await mountConsole();

  const fetched = api.get.mock.calls
    .map(([path]) => path)
    .filter((path) => typeof path === "string" && path.includes("audio-control"));
  expect(fetched.length).toBeGreaterThan(0);
  for (const path of fetched) {
    expect(path).toContain("/broadcast/sessions/77/audio-control");
  }
});

// ===========================================================================
// The live state must not outlive the broadcast
// ===========================================================================
test("stopping clears the restored campaign and selection", async () => {
  mockBroadcast = providerState({
    live: true, session: LIVE_SESSION, targets: LIVE_TARGETS,
  });
  const { rerender } = await mountConsole();
  expect(screen.getByTestId("campaign-name-input").value).toBe("Morning Offer");

  // The broadcast ends - Stop, or somebody else's Emergency Stop.
  mockBroadcast = providerState({ live: false });
  await act(async () => { rerender(<BroadcastConsole />); });

  expect(screen.getByTestId("campaign-name-input").value).toBe("");
  for (const code of ["BP", "RG", "VP"]) {
    expect(screen.getByTestId(`store-checkbox-${code}`).checked).toBe(false);
  }
});

test("a new draft after a stop does not inherit the finished broadcast",
     async () => {
  mockBroadcast = providerState({
    live: true, session: LIVE_SESSION, targets: LIVE_TARGETS,
  });
  const { unmount } = await mountConsole();
  unmount();

  // Emergency Stop leaves no session at all; coming back must show no ghost.
  mockBroadcast = providerState({ live: false });
  await mountConsole();

  expect(screen.getByTestId("campaign-name-input").value).toBe("");
  expect(screen.getByTestId("target-mode-select").value).toBe("selected");
  expect(screen.getByTestId("store-checkbox-BP").checked).toBe(false);
});

test("with no active session the Console opens as an ordinary empty draft",
     async () => {
  await mountConsole();
  expect(screen.getByTestId("campaign-name-input").value).toBe("");
  expect(screen.getByTestId("campaign-name-input").disabled).toBe(false);
  expect(screen.getByTestId("store-checkbox-BP").checked).toBe(false);
});

test("another operator's broadcast never populates this Console", async () => {
  // /broadcast/current only ever returns the caller's OWN session, so a
  // colleague being on air must leave this form untouched. Modelled as the
  // provider reporting somebody else's session in `active.sessions` while
  // `current.live` stays false.
  mockBroadcast = providerState({ live: false });
  mockBroadcast.active.sessions = [{
    session_id: 999, campaign_name: "Bob's Campaign",
    owner_username: "bob", target_store_ids: [101, 102],
  }];
  await mountConsole();

  expect(screen.getByTestId("campaign-name-input").value).toBe("");
  expect(screen.getByTestId("store-checkbox-BP").checked).toBe(false);
});
