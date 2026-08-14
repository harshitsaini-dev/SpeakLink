/**
 * Frontend menu hiding is never the security boundary - the backend
 * independently enforces the same permission on every request. This only
 * tests that the frontend's OWN routing decision (which link is shown, which
 * route a direct URL visit is blocked and redirected to) is correct, using
 * the exact permission codes the backend's /auth/permissions endpoint
 * returns.
 */
import { MENU_PERMISSION_BY_PATH, firstAllowedRoute } from "./menuPermissions";

test("every top-level nav route names its menu permission", () => {
  expect(MENU_PERMISSION_BY_PATH).toEqual({
    "/console": "menu.broadcast.view",
    // Supervision has its OWN permission rather than sharing the Console's.
    // An ordinary Broadcaster holds menu.broadcast.view and must not reach
    // the page that lists every other operator's broadcast.
    "/active-broadcasts": "broadcast.active_view",
    // Its own menu permission. Looking at what is playing is not the same
    // capability as running a broadcast, and a VIEWER holds the first
    // without the second.
    "/announcements": "menu.announcements.view",
    // Same subject as the live page, so the same permission: a second right
    // would only create a way to see one and not the other.
    "/announcement-history": "menu.announcements.view",
    "/stores": "menu.stores.view",
    "/history": "menu.history.view",
    "/receivers": "menu.receivers.view",
    // The fleet-wide Devices view shows the same Receivers from another angle,
    // so it deliberately shares menu.receivers.view rather than inventing a
    // second permission that could grant one view and withhold the other.
    "/devices": "menu.receivers.view",
    "/logs": "menu.logs.view",
    "/users": "menu.users.view",
  });
});

test("a VIEWER-shaped permission set redirects away from /users to the first allowed route", () => {
  const viewerPermissions = new Set([
    "menu.broadcast.view", "menu.stores.view", "menu.receivers.view",
    "menu.history.view", "menu.logs.view",
  ]);
  const can = (code) => viewerPermissions.has(code);

  expect(can(MENU_PERMISSION_BY_PATH["/users"])).toBe(false);
  expect(firstAllowedRoute(can)).toBe("/console");
});

test("a BROADCASTER-shaped permission set lands on /console, not /stores", () => {
  const broadcasterPermissions = new Set([
    "menu.broadcast.view", "menu.history.view", "menu.receivers.view",
  ]);
  const can = (code) => broadcasterPermissions.has(code);

  expect(firstAllowedRoute(can)).toBe("/console");
  expect(can(MENU_PERMISSION_BY_PATH["/stores"])).toBe(false);
});

test("an account with no operational menu permission at all falls back to Change Password", () => {
  const can = () => false;
  expect(firstAllowedRoute(can)).toBe("/account/password");
});

// ===========================================================================
// Active Broadcasts supervision
// ===========================================================================
test("an ordinary Broadcaster cannot reach the supervision page", () => {
  // The exact permission set the backend gives a BROADCASTER by default.
  const broadcaster = new Set([
    "menu.broadcast.view", "broadcast.start", "broadcast.stop",
    "menu.history.view", "menu.receivers.view", "menu.stores.view",
  ]);
  const can = (code) => broadcaster.has(code);

  expect(can(MENU_PERMISSION_BY_PATH["/active-broadcasts"])).toBe(false);
});

test("an explicit grant of broadcast.active_view opens the route", () => {
  const granted = new Set([
    "menu.broadcast.view", "broadcast.start", "broadcast.stop",
    "broadcast.active_view",
  ]);
  const can = (code) => granted.has(code);

  expect(can(MENU_PERMISSION_BY_PATH["/active-broadcasts"])).toBe(true);
});

test("an account with ONLY supervision rights still lands somewhere it may go", () => {
  // Holding active_view without menu.broadcast.view is unusual but legal, and
  // the landing route must not send them to a page they cannot open.
  const can = (code) => code === "broadcast.active_view";
  expect(firstAllowedRoute(can)).toBe("/active-broadcasts");
});
