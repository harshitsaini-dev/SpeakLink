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
  "/stores": "menu.stores.view",
  "/history": "menu.history.view",
  "/receivers": "menu.receivers.view",
  "/logs": "menu.logs.view",
  "/users": "menu.users.view",
};

/** The route a signed-in account should land on if its current one is denied. */
export const FIRST_ALLOWED_ROUTE = [
  "/console", "/stores", "/history", "/receivers", "/logs", "/users",
];

export function firstAllowedRoute(can) {
  return FIRST_ALLOWED_ROUTE.find((path) => can(MENU_PERMISSION_BY_PATH[path])) || "/account/password";
}
