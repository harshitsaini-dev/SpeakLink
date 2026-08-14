/**
 * The sidebar, grouped by what a person is trying to DO.
 *
 * A flat list of nine links made the reader scan all nine every time, and put
 * "Broadcast Console" - opened many times a day - beside "System Logs", which
 * is opened when something has already gone wrong.
 */
import React from "react";
import { render, screen } from "@testing-library/react";

// react-router-dom 7 ships ESM only, which CRA's jest cannot resolve, so the
// three things Layout uses are stubbed as what they render anyway. `virtual`
// because jest still RESOLVES a mocked path, and resolving this one is the
// thing that fails. What is asserted here - which links exist and how they
// are grouped - does not depend on real routing; that is proven in Playwright.
jest.mock("react-router-dom", () => ({
  NavLink: ({ to, children, className, ...rest }) => (
    <a href={to} className={typeof className === "function"
                            ? className({ isActive: false }) : className}
       {...rest}>{children}</a>
  ),
  Outlet: () => null,
  useNavigate: () => jest.fn(),
}), { virtual: true });

const permissions = { current: [] };
jest.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({
    user: { username: "founder", role: "OWNER" },
    logout: jest.fn(),
    can: (code) => permissions.current.includes(code),
  }),
}));
jest.mock("@/contexts/RecordingPlaybackContext", () => ({
  useRecordingPlayback: () => ({
    active: null, playToken: jest.fn(), pauseToken: jest.fn(),
    stopPlayback: jest.fn(),
  }),
}));
jest.mock("@/components/EmergencyStopControl", () => () => null);
jest.mock("@/components/RecordingPlayer", () => ({
  __esModule: true, default: () => null, PLAYER_BAR_HEIGHT: 0,
}));

const Layout = require("./Layout").default;

const EVERYTHING = [
  "menu.broadcast.view", "broadcast.active_view", "menu.announcements.view",
  "menu.stores.view", "menu.receivers.view", "menu.history.view",
  "menu.logs.view", "menu.users.view",
];

function renderLayout(codes = EVERYTHING) {
  permissions.current = codes;
  return render(<Layout />);
}

test("the navigation is grouped, and the groups are in the order of the day", () => {
  renderLayout();
  const headings = Array.from(
    document.querySelectorAll('[data-testid^="nav-group-"]'))
    .map((group) => group.getAttribute("data-testid"));
  expect(headings).toEqual([
    "nav-group-live", "nav-group-master", "nav-group-records",
    "nav-group-administration",
  ]);
});

test("a group whose every link is hidden does not render its heading", () => {
  // A heading over an empty space tells a reader something is missing without
  // telling them what - worse than the group not existing for that account.
  renderLayout(["menu.broadcast.view"]);

  expect(screen.getByTestId("nav-group-live")).toBeTruthy();
  expect(screen.queryByTestId("nav-group-master")).toBeNull();
  expect(screen.queryByTestId("nav-group-records")).toBeNull();
  // Administration survives: Change Password needs no permission, because
  // read-only does not mean unable to secure your own account.
  expect(screen.getByTestId("nav-group-administration")).toBeTruthy();
  expect(screen.queryByTestId("nav-users")).toBeNull();
});

test("every link still reaches its page after the regrouping", () => {
  renderLayout();
  for (const testid of ["nav-dashboard", "nav-console", "nav-active-broadcasts", "nav-announcements",
                        "nav-stores", "nav-receivers", "nav-devices",
                        "nav-announcement-templates", "nav-announcement-recordings",
                        "nav-history", "nav-logs", "nav-users", "nav-password"]) {
    expect(screen.getByTestId(testid)).toBeTruthy();
  }
});
