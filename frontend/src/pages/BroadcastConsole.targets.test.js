/**
 * Broadcast Console reads the TARGET catalog, not the Store Management list.
 *
 * The operator report was that a User without "View Store Management" also
 * lost the Store list in Broadcast Console. The cause was one endpoint doing
 * two jobs: the Console built its target table from GET /stores, which is
 * guarded by menu.stores.view, so the fetch 403'd and the table rendered
 * empty with no explanation.
 *
 * These tests assert the CALL the Console makes, because that is where the
 * coupling lived. Asserting only that rows appear would keep passing if
 * someone pointed it back at /stores and the mock happened to answer.
 */
import React from "react";
import { render, screen, act, cleanup, fireEvent } from "@testing-library/react";
import BroadcastConsole from "./BroadcastConsole";

const STORES = [
  { id: 101, store_code: "BP", store_name: "Bindapur", city: "DELHI",
    region: "NORTH", is_online_store: false, status: "online" },
  { id: 102, store_code: "RG", store_name: "Rajgarh", city: "DELHI",
    region: "NORTH", is_online_store: false, status: "offline" },
];

let mockPermissions;

jest.mock("react-router-dom", () => ({
  Link: ({ to, children, ...rest }) => <a href={to} {...rest}>{children}</a>,
}), { virtual: true });
// One STABLE object, not a fresh literal per call. The Console's load callback
// lists `loadBroadcast` in its useCallback deps, so returning a new object on
// every render changes the dep every render and the effect re-runs for ever -
// which shows up as a 5s test timeout, not as a loop anyone can see.
let mockBroadcast;
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

async function renderConsole(permissions = ["broadcast.start", "broadcast.stop"]) {
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
    broadcasterStatus: "idle",
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
  // Installed here rather than in jest.fn(impl): CRA sets resetMocks, which
  // strips an implementation passed at construction before every test.
  api.get.mockImplementation((path) => {
    if (path === "/broadcast/target-stores") {
      return Promise.resolve({
        data: { stores: STORES, regions: ["NORTH"], cities: ["DELHI"] },
      });
    }
    return Promise.resolve({ data: {} });
  });
  api.post.mockResolvedValue({ data: {} });
});

afterEach(cleanup);

test("the Console asks for broadcast targets, never the Store Management list", async () => {
  await renderConsole();
  const paths = api.get.mock.calls.map(([path]) => path);
  expect(paths).toContain("/broadcast/target-stores");
  expect(paths).not.toContain("/stores");
  expect(paths).not.toContain("/stores/meta/regions-cities");
});

test("targets render for an operator with no Store Management permission", async () => {
  // Exactly the operator's account: may broadcast, may not manage Stores.
  await renderConsole(["broadcast.start", "broadcast.stop"]);
  expect(mockPermissions.has("menu.stores.view")).toBe(false);
  expect(screen.getByTestId("store-row-BP")).toBeTruthy();
  expect(screen.getByTestId("store-row-RG")).toBeTruthy();
});

test("region options come from the scoped target response", async () => {
  await renderConsole();
  // The Region picker only exists in Region mode, so switch to it first -
  // otherwise this asserts against markup the Console never rendered.
  await act(async () => {
    fireEvent.change(screen.getByTestId("target-mode-select"),
                     { target: { value: "region" } });
  });
  // Derived from the returned Stores, so a scoped operator is never offered a
  // region they hold no Store in.
  expect(screen.getByRole("option", { name: "NORTH" })).toBeTruthy();
});
