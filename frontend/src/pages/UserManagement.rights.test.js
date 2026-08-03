/**
 * The Rights control is shown by CAPABILITY, not by role name.
 *
 * An OWNER granted an ADMIN "Manage User Rights" and nothing changed on
 * screen. The backend was refusing the endpoint (fixed separately), but so was
 * this page: the button was gated on `myRole === "OWNER"`, so even once the
 * permission worked there was no way to reach it.
 *
 * These tests drive the rendered DOM rather than inspecting props, because the
 * defect was that a control did not appear for someone entitled to it. They
 * deliberately cover the negative cases too - an ADMIN without the permission,
 * an OWNER row, and the actor's own row - since "show it to everyone" would
 * satisfy the positive test alone.
 */
import React from "react";
import { render, screen, act, cleanup } from "@testing-library/react";
import UserManagement from "./UserManagement";

const OWNER_ROW = { id: 1, username: "founder", display_name: "Founder",
                    role: "OWNER", is_active: true, lifecycle_state: "active" };
const ADMIN_ROW = { id: 2, username: "boss", display_name: "Boss",
                    role: "ADMIN", is_active: true, lifecycle_state: "active" };
const CASTER_ROW = { id: 3, username: "caster", display_name: "Caster",
                     role: "BROADCASTER", is_active: true, lifecycle_state: "active" };

let mockMe;
let mockPermissions;

jest.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: mockMe, can: (code) => mockPermissions.has(code) }),
}));
jest.mock("@/lib/api", () => ({
  api: { get: jest.fn(), post: jest.fn(), put: jest.fn(), delete: jest.fn() },
}));
jest.mock("@/lib/adminList", () => ({
  ...jest.requireActual("@/lib/adminList"),
  useAdminList: () => ({
    items: mockRows,
    total: mockRows.length,
    page: 1,
    pageSize: 50,
    filters: { q: "", role: "", state: "" },
    setFilter: jest.fn(),
    setPage: jest.fn(),
    reload: jest.fn(async () => {}),
    loading: false,
    error: "",
  }),
}));

// eslint-disable-next-line import/first
const { api } = require("@/lib/api");

let mockRows;

async function renderPage({ me, permissions, rows }) {
  mockMe = me;
  mockPermissions = new Set(permissions);
  mockRows = rows;
  render(<UserManagement />);
  await act(async () => {});
}

beforeEach(() => {
  jest.clearAllMocks();
  api.get.mockImplementation((path) => {
    if (path === "/receivers/filter-options") {
      return Promise.resolve({ data: { regions: [], cities: [], stores: [] } });
    }
    return Promise.resolve({ data: {} });
  });
});

afterEach(cleanup);

test("an ADMIN holding Manage User Rights sees Rights on an eligible User", async () => {
  await renderPage({
    me: ADMIN_ROW,
    permissions: ["menu.users.view", "users.permissions.manage"],
    rows: [CASTER_ROW],
  });
  expect(screen.getByTestId("rights-caster")).toBeTruthy();
});

test("an ADMIN without the permission sees no Rights control", async () => {
  await renderPage({
    me: ADMIN_ROW,
    permissions: ["menu.users.view"],
    rows: [CASTER_ROW],
  });
  expect(screen.queryByTestId("rights-caster")).toBeNull();
});

test("Rights is never offered on an OWNER row", async () => {
  await renderPage({
    me: ADMIN_ROW,
    permissions: ["menu.users.view", "users.permissions.manage"],
    rows: [OWNER_ROW],
  });
  expect(screen.queryByTestId("rights-founder")).toBeNull();
});

test("Rights is never offered on the actor's own row", async () => {
  // Self-escalation: the server refuses it, and the button must not invite it.
  await renderPage({
    me: ADMIN_ROW,
    permissions: ["menu.users.view", "users.permissions.manage"],
    rows: [ADMIN_ROW],
  });
  expect(screen.queryByTestId("rights-boss")).toBeNull();
});

test("an ADMIN may not manage a same-level ADMIN's rights", async () => {
  const peer = { ...ADMIN_ROW, id: 9, username: "peer", display_name: "Peer" };
  await renderPage({
    me: ADMIN_ROW,
    permissions: ["menu.users.view", "users.permissions.manage"],
    rows: [peer],
  });
  expect(screen.queryByTestId("rights-peer")).toBeNull();
});

test("an OWNER still sees Rights exactly as before", async () => {
  await renderPage({
    me: OWNER_ROW,
    permissions: ["menu.users.view", "users.permissions.manage"],
    rows: [CASTER_ROW],
  });
  expect(screen.getByTestId("rights-caster")).toBeTruthy();
});
