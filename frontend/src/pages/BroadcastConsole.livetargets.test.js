/**
 * Adding and removing ONE Store while the broadcast is on air.
 *
 * These are tests about honesty as much as wiring. The console has two
 * different facts about a Store and they are easy to conflate: lifecycle_state
 * says whether the Store is IN the broadcast, play_status says what its
 * Receiver last reported about sound. A removed Store keeps its last
 * play_status for ever, so a console that counted targets by play_status would
 * go on showing a shop as receiving an announcement it was taken out of.
 *
 * The other thing proved here is that the page never offers an action the
 * backend is going to refuse: no Add for an offline Receiver, none for a Store
 * another broadcast is holding.
 */
import React from "react";
import { render, screen, act, cleanup, fireEvent, waitFor } from "@testing-library/react";
import BroadcastConsole from "./BroadcastConsole";

const STORES = [
  { id: 101, store_code: "AAA", store_name: "North Shop", city: "DELHI",
    region: "NORTH", is_online_store: false, status: "online" },
  { id: 102, store_code: "BBB", store_name: "South Shop", city: "DELHI",
    region: "NORTH", is_online_store: false, status: "online" },
  { id: 103, store_code: "CCC", store_name: "Dark Shop", city: "DELHI",
    region: "NORTH", is_online_store: false, status: "offline" },
];

let mockPermissions;
let mockBroadcast;

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
  api: { get: jest.fn(), post: jest.fn(), delete: jest.fn() },
  getToken: () => "t",
  wsUrl: (p) => `ws://localhost:8000${p}`,
}));

// eslint-disable-next-line import/first
const { api } = require("@/lib/api");

const SESSION = {
  id: 77, campaign_name: "Evening announcement", status: "live",
  target_mode: "selected", started_at: new Date().toISOString(),
};

function target(storeId, overrides = {}) {
  return {
    id: storeId, store_id: storeId, play_status: "audio_receiving",
    lifecycle_state: "ACTIVE", current_generation: 1, ...overrides,
  };
}

/** A live broadcast reaching whichever Stores the test names. */
function goLive(targets, { onlineIds = [101, 102] } = {}) {
  mockBroadcast.isLive = true;
  mockBroadcast.current = {
    live: true, session: SESSION, targets,
    online_receivers: onlineIds, ready_receivers: onlineIds,
  };
}

async function renderConsole(permissions = ["broadcast.start", "broadcast.stop",
                                            "broadcast.store_delivery"]) {
  mockPermissions = new Set(permissions);
  render(<BroadcastConsole />);
  await act(async () => {});
}

beforeEach(() => {
  jest.clearAllMocks();
  mockBroadcast = {
    current: { live: false },
    load: jest.fn(async () => {}),
    isLive: false,
    meter: 0,
    micLevels: null,
    broadcasterStatus: "idle",
    micVolumePercent: 100,
    micMuted: false,
    setMicVolume: jest.fn(),
    setMicMute: jest.fn(),
    micEffectivelySilent: false,
    error: "",
    setError: jest.fn(),
    startBroadcast: jest.fn(async () => {}),
    stopBroadcast: jest.fn(async () => {}),
    emergencyStop: jest.fn(async () => ({ ok: true })),
    active: {
      mine: null, sessions: [], busy_store_ids: [], may_view_ownership: false,
      may_view_targets: false, may_manage_active: false, active_count: null,
    },
    isStoreBusyForOthers: () => false,
  };
  api.get.mockImplementation((path) => {
    if (path === "/broadcast/target-stores") {
      return Promise.resolve({
        data: { stores: STORES, regions: ["NORTH"], cities: ["DELHI"] },
      });
    }
    return Promise.resolve({ data: {} });
  });
  api.post.mockResolvedValue({ data: {} });
  api.delete.mockResolvedValue({ data: {} });
});

afterEach(cleanup);


// ===========================================================================
// The controls appear only where they mean something
// ===========================================================================

test("no Add or Remove control exists before the broadcast is live", async () => {
  await renderConsole();
  expect(screen.queryByTestId("add-store-AAA")).toBeNull();
  expect(screen.queryByTestId("remove-store-AAA")).toBeNull();
});

test("a Store in the broadcast offers Remove, one outside it offers Add", async () => {
  goLive([target(101)]);
  await renderConsole();
  expect(screen.getByTestId("remove-store-AAA")).toBeTruthy();
  expect(screen.queryByTestId("add-store-AAA")).toBeNull();
  expect(screen.getByTestId("add-store-BBB")).toBeTruthy();
  expect(screen.queryByTestId("remove-store-BBB")).toBeNull();
});

test("an offline Receiver is not offered Add, and says why", async () => {
  // The backend refuses this anyway. A button that fails when pressed is a
  // promise the page already knows it cannot keep.
  goLive([target(101)]);
  await renderConsole();
  expect(screen.queryByTestId("add-store-CCC")).toBeNull();
  expect(screen.getByTestId("add-blocked-CCC").textContent).toMatch(/offline/i);
});

test("a Store held by another broadcast is not offered Add, and never names it", async () => {
  mockBroadcast.isStoreBusyForOthers = (id) => id === 102;
  goLive([target(101)]);
  await renderConsole();
  expect(screen.queryByTestId("add-store-BBB")).toBeNull();
  const blocked = screen.getByTestId("add-blocked-BBB");
  expect(blocked.textContent).toMatch(/another broadcast/i);
  // WHAT, never WHO.
  expect(blocked.textContent).not.toMatch(/campaign|Evening|operator/i);
});


// ===========================================================================
// What the buttons actually do
// ===========================================================================

test("Add posts to this session's targets and then re-reads the broadcast", async () => {
  goLive([target(101)]);
  await renderConsole();

  await act(async () => { fireEvent.click(screen.getByTestId("add-store-BBB")); });

  expect(api.post).toHaveBeenCalledWith("/broadcast/sessions/77/targets",
                                        { store_id: 102 });
  // Re-read rather than guessed: whether the Store actually joined is the
  // backend's answer, and a console that assumed success would show a shop as
  // receiving audio because a request was sent.
  expect(mockBroadcast.load).toHaveBeenCalled();
});

test("Remove deletes exactly that Store from exactly this session", async () => {
  goLive([target(101), target(102)]);
  await renderConsole();

  await act(async () => { fireEvent.click(screen.getByTestId("remove-store-BBB")); });

  expect(api.delete).toHaveBeenCalledWith("/broadcast/sessions/77/targets/102");
  expect(mockBroadcast.load).toHaveBeenCalled();
});

test("a refusal is reported on the row, naming nothing else", async () => {
  api.post.mockRejectedValueOnce({
    response: { data: { detail: "BBB did not report ready in time." } } });
  goLive([target(101)]);
  await renderConsole();

  await act(async () => { fireEvent.click(screen.getByTestId("add-store-BBB")); });

  await waitFor(() => {
    expect(screen.getByTestId("target-error-BBB").textContent)
      .toMatch(/did not report ready/);
  });
  // The other row is untouched - one Store failing to join is not a page-wide
  // failure, and marking it as one would hide which shop refused.
  expect(screen.queryByTestId("target-error-AAA")).toBeNull();
});

test("only one add or remove can be in flight at a time", async () => {
  // Two adds in flight each wait on a different Receiver, and the operator has
  // no way to tell which row the next answer belongs to.
  let release;
  api.post.mockImplementationOnce(() => new Promise((resolve) => { release = resolve; }));
  goLive([target(101)]);
  await renderConsole();

  await act(async () => { fireEvent.click(screen.getByTestId("add-store-BBB")); });

  expect(screen.getByTestId("add-store-BBB").textContent).toMatch(/Adding/);
  expect(screen.getByTestId("remove-store-AAA").disabled).toBe(true);

  await act(async () => { release({ data: {} }); });
  await waitFor(() => {
    expect(screen.getByTestId("remove-store-AAA").disabled).toBe(false);
  });
});


// ===========================================================================
// lifecycle_state is not play_status
// ===========================================================================

test("a REMOVED Store stops being counted, whatever its last play_status says", async () => {
  // The removed row keeps play_status audio_receiving for ever - that WAS true
  // when the Receiver last spoke. Counting it as a target would tell the
  // operator the announcement is still reaching a shop they took out.
  goLive([target(101), target(102, { lifecycle_state: "REMOVED",
                                     play_status: "audio_receiving" })]);
  await renderConsole();

  expect(screen.getByTestId("stat-selected").textContent).toContain("1");
  // And the row offers Add again, not Remove.
  expect(screen.getByTestId("add-store-BBB")).toBeTruthy();
  expect(screen.queryByTestId("remove-store-BBB")).toBeNull();
});

test("a Store still settling says so instead of looking finished", async () => {
  goLive([target(102, { lifecycle_state: "PREPARING", play_status: "pending" })]);
  await renderConsole();
  expect(screen.getByTestId("target-state-BBB").textContent).toContain("PREPARING");
});

// ===========================================================================
// Pause and Resume
// ===========================================================================

test("a Store in the broadcast is offered Pause as well as Remove", async () => {
  // Both, deliberately. With only Remove on the row, a thirty-second silence
  // becomes a removal - which releases the Store and lets another broadcast
  // take the shop.
  goLive([target(101)]);
  await renderConsole();
  expect(screen.getByTestId("pause-store-AAA")).toBeTruthy();
  expect(screen.getByTestId("remove-store-AAA")).toBeTruthy();
});

test("Pause posts to the pause route for exactly that Store", async () => {
  goLive([target(101), target(102)]);
  await renderConsole();

  await act(async () => { fireEvent.click(screen.getByTestId("pause-store-BBB")); });

  expect(api.post).toHaveBeenCalledWith(
    "/broadcast/sessions/77/targets/102/pause");
  expect(mockBroadcast.load).toHaveBeenCalled();
});

test("a paused Store offers Resume instead of Pause, and says it is paused", async () => {
  goLive([target(101), target(102, { lifecycle_state: "PAUSED" })]);
  await renderConsole();

  expect(screen.getByTestId("resume-store-BBB")).toBeTruthy();
  expect(screen.queryByTestId("pause-store-BBB")).toBeNull();
  expect(screen.getByTestId("target-state-BBB").textContent).toContain("PAUSED");
  // Still counted as a target: it has not left the broadcast.
  expect(screen.getByTestId("stat-selected").textContent).toContain("2");
});

test("Resume posts to the resume route", async () => {
  goLive([target(102, { lifecycle_state: "PAUSED" })]);
  await renderConsole();

  await act(async () => { fireEvent.click(screen.getByTestId("resume-store-BBB")); });

  expect(api.post).toHaveBeenCalledWith(
    "/broadcast/sessions/77/targets/102/resume");
});

test("a refusal to resume is reported on the row", async () => {
  api.post.mockRejectedValueOnce({ response: { data: {
    detail: "BBB has no Receiver connected, so it cannot be resumed. It is still paused." } } });
  goLive([target(102, { lifecycle_state: "PAUSED" })]);
  await renderConsole();

  await act(async () => { fireEvent.click(screen.getByTestId("resume-store-BBB")); });

  await waitFor(() => {
    expect(screen.getByTestId("target-error-BBB").textContent)
      .toMatch(/still paused/);
  });
});

// ===========================================================================
// Zone actions
// ===========================================================================

test("zone actions appear only while live", async () => {
  await renderConsole();
  expect(screen.queryByTestId("zone-actions")).toBeNull();

  cleanup();
  goLive([target(101)]);
  await renderConsole();
  expect(screen.getByTestId("zone-actions")).toBeTruthy();
});

test("no Zone and no City means no action, and says why", async () => {
  // An empty selector would mean the whole estate. The backend refuses it;
  // the page does not offer it in the first place.
  goLive([target(101)]);
  await renderConsole();

  expect(screen.getByTestId("zone-pause").disabled).toBe(true);
  expect(screen.getByTestId("zone-needs-scope")).toBeTruthy();
});

test("choosing a Zone enables the actions and sends the selector", async () => {
  api.post.mockResolvedValueOnce({ data: {
    action: "pause", requested: 2, succeeded: 2, results: [] } });
  goLive([target(101), target(102)]);
  await renderConsole();

  await act(async () => {
    fireEvent.change(screen.getByTestId("zone-action-region"),
                     { target: { value: "NORTH" } });
  });
  await act(async () => { fireEvent.click(screen.getByTestId("zone-pause")); });

  expect(api.post).toHaveBeenCalledWith(
    "/broadcast/sessions/77/targets/bulk",
    { action: "pause", region: "NORTH" });
  expect(screen.getByTestId("zone-result-summary").textContent)
    .toMatch(/2 of 2/);
  expect(mockBroadcast.load).toHaveBeenCalled();
});

test("the refusals are listed by Store, and the successes are not", async () => {
  // A wall of green ticks buries the rows that need attention.
  api.post.mockResolvedValueOnce({ data: {
    action: "pause", requested: 2, succeeded: 1,
    results: [
      { store_id: 101, ok: true, lifecycle_state: "PAUSED", detail: null },
      { store_id: 102, ok: false, lifecycle_state: null,
        detail: "Only a Store that is currently receiving can be paused." },
    ] } });
  goLive([target(101), target(102)]);
  await renderConsole();

  await act(async () => {
    fireEvent.change(screen.getByTestId("zone-action-city"),
                     { target: { value: "DELHI" } });
  });
  await act(async () => { fireEvent.click(screen.getByTestId("zone-pause")); });

  expect(screen.getByTestId("zone-failed-102").textContent)
    .toMatch(/BBB: Only a Store that is currently receiving/);
  expect(screen.queryByTestId("zone-failed-101")).toBeNull();
  expect(screen.getByTestId("zone-result-summary").textContent).toMatch(/1 of 2/);
});
