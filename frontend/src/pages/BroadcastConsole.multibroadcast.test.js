/**
 * What the Broadcast Console shows when several operators are on air.
 *
 * The console renders three things it did not before: a busy marker on Stores
 * another broadcast holds, a panel for your own live broadcast, and - only for
 * accounts holding broadcast.active_view - a COMPACT BADGE linking to the
 * Active Broadcasts page.
 *
 * That badge replaced a full cross-user table. The table was readable with one
 * concurrent broadcast and unusable with twenty, and it grew without bound
 * behind the controls an operator actually came to the Console for. The tests
 * below therefore assert both halves: that the old list is gone, and that the
 * badge's height does not depend on the number of live broadcasts.
 *
 * The privacy tests here assert on the RENDERED DOM rather than on props,
 * because the failure being guarded against is a name reaching a screen. They
 * also assert that Emergency Stop has its own confirmation: reusing the
 * ordinary Start dialog for an action that stops every operator's broadcast is
 * how it becomes a reflex.
 */
import React from "react";
import { render, screen, act, fireEvent, cleanup } from "@testing-library/react";
import BroadcastConsole from "./BroadcastConsole";

const STORES = [
  { id: 101, store_code: "BP", store_name: "Testville North", city: "DELHI",
    region: "NORTH", status: "online" },
  { id: 102, store_code: "RG", store_name: "Testville South", city: "DELHI",
    region: "NORTH", status: "online" },
];

let mockBroadcast;
let mockPermissions;

// react-router-dom 7 ships ESM only, which CRA's jest cannot resolve. The
// Console needs exactly one thing from it - Link - so it is stubbed as the
// anchor it renders anyway. The href assertion below is therefore still a
// real assertion about the destination, and the actual routing is proven in
// the Playwright suite where a real browser resolves it.
// `virtual` because jest still RESOLVES a mocked module path, and resolving
// this one is the very thing that fails.
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

function baseBroadcast(overrides = {}) {
  const active = {
    mine: null, sessions: [], busy_store_ids: [], may_view_ownership: false,
    may_view_targets: false, may_manage_active: false, active_count: null,
    ...(overrides.active || {}),
  };
  return {
    current: overrides.current ?? { live: false },
    load: jest.fn(async () => {}),
    isLive: Boolean(overrides.isLive),
    meter: 0,
    broadcasterStatus: "idle",
    error: "",
    setError: jest.fn(),
    startBroadcast: jest.fn(async () => {}),
    stopBroadcast: jest.fn(async () => {}),
    emergencyStop: jest.fn(async () => ({ ok: true, session_ids: [1, 2] })),
    active,
    isStoreBusyForOthers: (id) => (
      (active.busy_store_ids || []).includes(id)
      && !(active.mine?.target_store_ids || []).includes(id)
    ),
    ...(overrides.extra || {}),
  };
}

async function renderConsole(overrides = {}, permissions = ["broadcast.start", "broadcast.stop"]) {
  mockBroadcast = baseBroadcast(overrides);
  mockPermissions = new Set(permissions);
  render(<BroadcastConsole />);
  await act(async () => {});
  return mockBroadcast;
}

beforeEach(() => {
  jest.clearAllMocks();
  // The implementation is installed HERE, not in the jest.fn() call above:
  // Create React App sets resetMocks, which strips an implementation passed to
  // jest.fn(impl) before every test and leaves api.get returning undefined.
  api.get.mockImplementation((path) => {
    if (path === "/stores") return Promise.resolve({ data: STORES });
    if (path === "/stores/meta/regions-cities") {
      return Promise.resolve({ data: { regions: ["NORTH"], cities: ["DELHI"] } });
    }
    return Promise.resolve({ data: {} });
  });
  api.post.mockResolvedValue({ data: {} });
});

// ===========================================================================
// Busy Stores
// ===========================================================================
test("a Store held by another broadcast is marked and cannot be selected", async () => {
  await renderConsole({ active: { busy_store_ids: [101] } });

  expect(screen.getByTestId("store-busy-BP")).toBeTruthy();
  expect(screen.getByTestId("store-checkbox-BP").disabled).toBe(true);
  // The free one is unaffected.
  expect(screen.queryByTestId("store-busy-RG")).toBeNull();
  expect(screen.getByTestId("store-checkbox-RG").disabled).toBe(false);
});

test("the busy marker names no operator and no campaign", async () => {
  await renderConsole({ active: { busy_store_ids: [101] } });

  // The console has its own "Campaign name" field, so a whole-page scan for
  // the word would be meaningless. What must be absent is another operator's
  // IDENTITY - and the busy marker itself must say only what, not who.
  const rendered = document.body.textContent.toLowerCase();
  for (const leak of ["alice", "bob", "in use by ", "owner:"]) {
    expect(rendered.includes(leak)).toBe(false);
  }

  const badge = screen.getByTestId("store-busy-BP");
  expect(badge.textContent.toLowerCase()).toContain("in use");
  expect(badge.getAttribute("title").toLowerCase())
    .toBe("bp is currently in use by another broadcast.");
});

test("a Store MY broadcast holds is not marked busy on my own console", async () => {
  await renderConsole({
    isLive: true,
    active: {
      busy_store_ids: [101],
      mine: { session_id: 5, campaign_name: "Mine", target_store_ids: [101],
              target_store_count: 1 },
    },
  });

  expect(screen.queryByTestId("store-busy-BP")).toBeNull();
});

test("another operator being live does not disable Start", async () => {
  await renderConsole({ isLive: false, active: { busy_store_ids: [101] } });

  // The Start control exists and is not globally suppressed just because a
  // broadcast is happening somewhere.
  expect(screen.getByTestId("start-broadcast-btn")).toBeTruthy();
});

// ===========================================================================
// My own broadcast panel
// ===========================================================================
test("my active broadcast is shown with its own details", async () => {
  await renderConsole({
    isLive: true,
    current: { live: true, session: { id: 5, started_at: null }, targets: [] },
    active: {
      busy_store_ids: [101],
      mine: { session_id: 5, campaign_name: "Diwali Offers",
              started_at: "2026-08-03T10:00:00+00:00",
              target_store_ids: [101], target_store_count: 1 },
    },
  });

  expect(screen.getByTestId("my-active-broadcast")).toBeTruthy();
  expect(screen.getByTestId("my-active-campaign").textContent).toBe("Diwali Offers");
  expect(screen.getByTestId("my-active-target-count").textContent).toBe("1");
});

test("no panel when I am not broadcasting", async () => {
  await renderConsole({ active: { busy_store_ids: [101] } });
  expect(screen.queryByTestId("my-active-broadcast")).toBeNull();
});

// ===========================================================================
// The compact badge that replaced the cross-user list
// ===========================================================================
// The Console used to render every other operator's broadcast as table rows.
// That was readable at one concurrent broadcast and unusable at twenty: the
// page where somebody speaks into a microphone was growing without bound
// behind the controls they came for. Supervision moved to its own page.
test("the cross-user Active Broadcast list is gone from the Console", async () => {
  await renderConsole({
    active: {
      busy_store_ids: [101], may_view_ownership: true, may_manage_active: true,
      active_count: 3,
      sessions: [{ session_id: 3, campaign_name: "Alice Campaign",
                   owner_username: "alice", target_store_count: 1 }],
    },
  }, ["broadcast.start", "broadcast.stop", "broadcast.view_ownership",
      "broadcast.active_view"]);

  expect(screen.queryByTestId("active-broadcasts-panel")).toBeNull();
  expect(screen.queryByTestId("active-session-3")).toBeNull();
  // Even when the context still carries a session, the Console must not name
  // the operator - that belongs on the supervision page now.
  expect(document.body.textContent.toLowerCase()).not.toContain("alice campaign");
});

test("an account without active_view gets no badge and no count", async () => {
  await renderConsole({
    active: { busy_store_ids: [101], may_manage_active: false, active_count: null },
  });

  expect(screen.queryByTestId("active-broadcasts-badge")).toBeNull();
});

test("the badge shows the backend's count and links to the page", async () => {
  await renderConsole({
    active: { busy_store_ids: [101], may_manage_active: true, active_count: 17 },
  }, ["broadcast.start", "broadcast.stop", "broadcast.active_view"]);

  expect(screen.getByTestId("active-broadcasts-badge")).toBeTruthy();
  expect(screen.getByTestId("active-broadcasts-count").textContent).toBe("17");
  expect(screen.getByTestId("active-broadcasts-link").getAttribute("href"))
    .toBe("/active-broadcasts");
});

test("the Console is the same height with 50 broadcasts as with 1", async () => {
  // The property the operator actually asked for, asserted as a property
  // rather than a screenshot: the badge renders ONE element whatever the
  // count says, so the page cannot grow with concurrency.
  const rowsAt = async (count) => {
    cleanup();   // two renders into one document would find two badges
    await renderConsole({
      active: { busy_store_ids: [], may_manage_active: true, active_count: count },
    }, ["broadcast.start", "broadcast.stop", "broadcast.active_view"]);
    const badge = screen.getByTestId("active-broadcasts-badge");
    return badge.querySelectorAll("*").length;
  };

  const withOne = await rowsAt(1);
  const withFifty = await rowsAt(50);
  expect(withFifty).toBe(withOne);
  expect(screen.getByTestId("active-broadcasts-count").textContent).toBe("50");
});

// ===========================================================================
// Emergency Stop
// ===========================================================================
test("a Broadcaster without the permission gets no Emergency Stop button", async () => {
  await renderConsole({}, ["broadcast.start", "broadcast.stop"]);
  expect(screen.queryByTestId("emergency-stop-btn")).toBeNull();
});

test("an account with the permission gets the button", async () => {
  await renderConsole({}, ["broadcast.start", "broadcast.emergency_stop"]);
  expect(screen.getByTestId("emergency-stop-btn")).toBeTruthy();
});

test("Emergency Stop asks its own confirmation naming ALL broadcasts", async () => {
  await renderConsole({}, ["broadcast.start", "broadcast.emergency_stop"]);

  await act(async () => {
    fireEvent.click(screen.getByTestId("emergency-stop-btn"));
  });

  const modal = screen.getByTestId("emergency-confirm-modal");
  const words = modal.textContent.toLowerCase();
  expect(words).toContain("all active");
  expect(words).toContain("other operators");
  // Not the ordinary Start confirmation.
  expect(screen.queryByTestId("confirm-start-btn")).toBeNull();
});

test("confirming reports how many were stopped", async () => {
  const broadcast = await renderConsole({}, ["broadcast.emergency_stop"]);

  await act(async () => {
    fireEvent.click(screen.getByTestId("emergency-stop-btn"));
  });
  await act(async () => {
    fireEvent.click(screen.getByTestId("emergency-confirm-btn"));
  });

  expect(broadcast.emergencyStop).toHaveBeenCalled();
  expect(screen.getByTestId("emergency-result").textContent).toContain("2");
});

test("a partial failure renders an error, never a success message", async () => {
  mockBroadcast = baseBroadcast({});
  mockPermissions = new Set(["broadcast.emergency_stop"]);
  const partial = new Error("SOME BROADCASTS ARE STILL LIVE. Not every broadcast could be stopped.");
  partial.emergencyIncomplete = true;
  partial.failedSessionIds = [2];
  mockBroadcast.emergencyStop = jest.fn(async () => { throw partial; });
  render(<BroadcastConsole />);
  await act(async () => {});

  await act(async () => {
    fireEvent.click(screen.getByTestId("emergency-stop-btn"));
  });
  await act(async () => {
    fireEvent.click(screen.getByTestId("emergency-confirm-btn"));
  });

  const result = screen.getByTestId("emergency-result");
  expect(result.textContent).toContain("STILL LIVE");
  expect(result.textContent.toLowerCase()).not.toContain("all broadcasts stopped");
});

test("cancelling the emergency confirmation stops nothing", async () => {
  const broadcast = await renderConsole({}, ["broadcast.emergency_stop"]);

  await act(async () => {
    fireEvent.click(screen.getByTestId("emergency-stop-btn"));
  });
  await act(async () => {
    fireEvent.click(screen.getByTestId("emergency-cancel-btn"));
  });

  expect(broadcast.emergencyStop).not.toHaveBeenCalled();
  expect(screen.queryByTestId("emergency-confirm-modal")).toBeNull();
});
