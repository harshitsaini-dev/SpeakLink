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
  "/console": "menu.broadcast.view",
  // Its own permission, not menu.broadcast.view: supervising everybody's
  // broadcasts is a different capability from running your own, and an
  // ordinary Broadcaster holds the second without the first.
  "/active-broadcasts": "broadcast.active_view",
  "/stores": "menu.stores.view",
  // The same permission that governs changing a Store's volume during a
  // broadcast. Seeing how loud the estate is and being able to change it
  // are one responsibility; a second permission here would only let the
  // two drift apart.
  "/master-volume": "store_audio.control",
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
  "/console", "/active-broadcasts", "/stores", "/master-volume", "/history", "/receivers", "/logs", "/users",
];

export function firstAllowedRoute(can) {
  return FIRST_ALLOWED_ROUTE.find((path) => can(MENU_PERMISSION_BY_PATH[path])) || "/account/password";
}
