// A sibling import rather than the "@/lib/api" alias used by the pages: the
// alias is a webpack resolution and jest does not share it, so aliasing here
// would mean either an untestable module or a change to the build config for
// something that is one directory away.
import { backendDisplayHost, isNetworkError } from "./api";

/**
 * Turn a failed sign-in into a message that says what actually happened.
 *
 * The login page used to do this:
 *
 *     setErr(e2?.response?.data?.detail || "Login failed");
 *
 * which is correct for a rejected password and badly wrong for everything else.
 * When RC4 shipped with a loopback backend URL baked into the bundle, every
 * login request died with ERR_CONNECTION_REFUSED before it left the browser -
 * and the page said "Login failed". The operator was told their credentials were
 * refused by a server that had never seen them, and the obvious next step from
 * there is to start resetting passwords, which in this system is a deliberate,
 * audited act performed offline.
 *
 * So the distinction is not cosmetic. A transport failure and a credential
 * rejection lead a person to two different rooms.
 *
 * Nothing here echoes a username, a password, a token or a response body beyond
 * the backend's own `detail` string, which is written by the API for display.
 */

export const NETWORK_MESSAGE_PREFIX = "Cannot reach the EchoCast backend";

export function loginErrorMessage(error) {
  // No response at all: the request never arrived. Name the address it tried,
  // because on a LAN the answer is almost always that the operator opened the
  // dashboard by an address the backend is not listening on.
  if (isNetworkError(error)) {
    return (
      `${NETWORK_MESSAGE_PREFIX} at ${backendDisplayHost()}. ` +
      "Your password has not been checked. Confirm HQ is running and that this " +
      "page was opened using the HQ machine's address."
    );
  }

  const status = error?.response?.status;

  // 429 covers both "too fast" and "this account is temporarily locked". The
  // backend deliberately answers the same way for both, because saying which one
  // applied would say whether the account exists. The wording is fixed here
  // rather than echoed, so no future server detail can leak a count, a threshold
  // or an unlock time into the page.
  if (status === 429) {
    return "Too many sign-in attempts. Please wait a while and try again.";
  }

  if (status === 401 || status === 403) {
    return error?.response?.data?.detail || "Incorrect username or password.";
  }

  // A response came back, so the backend is reachable; it just could not serve
  // this. Distinguished from the transport case so nobody goes looking for a
  // network fault that is not there.
  if (status >= 500) {
    return (
      "The EchoCast backend answered with an error. Your password has not been " +
      "checked. Check the HQ runtime log."
    );
  }

  return error?.response?.data?.detail || "Login failed";
}
