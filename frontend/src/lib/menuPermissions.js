/**
 * One map from route path to the menu permission it needs, shared by the
 * sidebar (which permission hides a link) and ProtectedRoute (which
 * permission blocks the route itself when visited directly by URL).
 *
 * A route missing from this map needs no permission beyond being signed in -
 * Change Password is the deliberate example: read-only does not mean unable
 * to secure your own account.
 */
export const MENU_PERMISSION_BY_PATH = {
  // The same permission as the Console. The dashboard summarises what the
  // Console does; a separate right would let somebody read the totals for
  // work they cannot see.
  "/dashboard": "menu.broadcast.view",
  "/console": "menu.broadcast.view",
  // Its own permission, not menu.broadcast.view: supervising everybody's
  // broadcasts is a different capability from running your own, and an
  // ordinary Broadcaster holds the second without the first.
  "/active-broadcasts": "broadcast.active_view",
  "/announcements": "menu.announcements.view",
  // The same permission as the live page. Whether a shop played something an
  // hour ago is the same subject as whether it is playing now, and a second
  // right would only create a way to see one and not the other.
  "/announcement-history": "menu.announcements.view",
  // Same subject again. A separate right here would let somebody see what is
  // playing and not what was planned, which is a distinction nobody could
  // explain.
  "/announcement-templates": "menu.announcements.view",
  "/announcement-recordings": "menu.announcements.view",
  "/stores": "menu.stores.view",
  "/history": "menu.history.view",
  "/receivers": "menu.receivers.view",
  // Same subject as /receivers, so the same menu permission gates it. A second
  // permission for the same data would only create a way to see one view and
  // not the other, which is a difference nobody could explain.
  "/devices": "menu.receivers.view",
  "/logs": "menu.logs.view",
  "/users": "menu.users.view",
};

/** The route a signed-in account should land on if its current one is denied. */
export const FIRST_ALLOWED_ROUTE = [
  // /console first, not /dashboard. This list answers "where does somebody
  // land when the page they asked for is denied", and landing a broadcaster
  // on a summary of work adds a click before they can do the work.
  "/console", "/dashboard", "/active-broadcasts", "/announcements", "/stores", "/history", "/receivers", "/logs", "/users",
];

export function firstAllowedRoute(can) {
  return FIRST_ALLOWED_ROUTE.find((path) => can(MENU_PERMISSION_BY_PATH[path])) || "/account/password";
}
