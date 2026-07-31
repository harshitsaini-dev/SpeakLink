/**
 * "Login failed" must not be what an unreachable backend looks like.
 *
 * On RC4 every login request died with ERR_CONNECTION_REFUSED before leaving the
 * browser, and the page reported "Login failed". That reads as "wrong password",
 * and the next thing an operator does is reset one - which in this system is a
 * deliberate, audited, offline act. The message sent them to the wrong room.
 */

import { loginErrorMessage, NETWORK_MESSAGE_PREFIX } from "./loginError";

/** What axios actually produces when the request never got a response. */
function connectionRefused() {
  const error = new Error("Network Error");
  error.code = "ERR_NETWORK";
  error.request = {};
  error.response = undefined;
  return error;
}

function withStatus(status, detail) {
  const error = new Error(`Request failed with status code ${status}`);
  error.request = {};
  error.response = { status, data: detail ? { detail } : {} };
  return error;
}

// ===========================================================================
// The transport failure
// ===========================================================================
test("a refused connection is reported as unreachable, not as a bad password", () => {
  const message = loginErrorMessage(connectionRefused());

  expect(message).toContain(NETWORK_MESSAGE_PREFIX);
  expect(message.toLowerCase()).not.toContain("incorrect");
  expect(message).not.toBe("Login failed");
});

test("the unreachable message says the password was not checked", () => {
  // The single most useful sentence: it stops a password reset that would not
  // have helped.
  expect(loginErrorMessage(connectionRefused()).toLowerCase())
    .toContain("has not been checked");
});

test("the unreachable message names the address that was tried", () => {
  // Asserted against the resolver's own answer rather than a hard-coded host.
  // Under jest the page is loopback and frontend/.env supplies a loopback
  // override, so the override is correctly honoured and the host is 127.0.0.1 -
  // hard-coding "localhost" here made the test fail for a reason that had
  // nothing to do with what it was checking.
  const { backendDisplayHost } = require("./api");
  const message = loginErrorMessage(connectionRefused());

  expect(message).toContain(backendDisplayHost());
  expect(message).toMatch(/:8000/);
});

test("a bare Network Error with no code is still a transport failure", () => {
  const error = new Error("Network Error");
  error.request = {};

  expect(loginErrorMessage(error)).toContain(NETWORK_MESSAGE_PREFIX);
});

// ===========================================================================
// The credential rejection
// ===========================================================================
test("a 401 is reported as a credential rejection", () => {
  const message = loginErrorMessage(withStatus(401));

  expect(message).not.toContain(NETWORK_MESSAGE_PREFIX);
  expect(message.toLowerCase()).toContain("incorrect username or password");
});

test("a 403 is reported as a credential rejection", () => {
  expect(loginErrorMessage(withStatus(403)).toLowerCase())
    .toContain("incorrect username or password");
});

test("the backend's own detail is preferred when it supplies one", () => {
  expect(loginErrorMessage(withStatus(401, "This account is disabled.")))
    .toBe("This account is disabled.");
});

// ===========================================================================
// Rate limiting keeps its fixed wording
// ===========================================================================
test("a 429 keeps the fixed wording and never echoes the server", () => {
  // Echoing the detail could leak a count, a threshold or an unlock time, and
  // whether an account exists.
  const message = loginErrorMessage(
    withStatus(429, "locked for 900s after 5 failures for user founder"));

  expect(message).toBe("Too many sign-in attempts. Please wait a while and try again.");
  expect(message).not.toContain("900");
  expect(message).not.toContain("founder");
});

// ===========================================================================
// A server fault is neither of the above
// ===========================================================================
test("a 500 says the backend answered with an error", () => {
  const message = loginErrorMessage(withStatus(500));

  expect(message).not.toContain(NETWORK_MESSAGE_PREFIX);
  expect(message.toLowerCase()).toContain("has not been checked");
});

// ===========================================================================
// Nothing is echoed that should not be
// ===========================================================================
test("no message contains a password, a token or a hash", () => {
  const cases = [
    connectionRefused(),
    withStatus(401, "bad password: hunter2"),
    withStatus(429),
    withStatus(500),
  ];

  for (const error of cases) {
    const message = loginErrorMessage(error);
    expect(message).not.toContain("$2b$");
    expect(message).not.toMatch(/eyJ[A-Za-z0-9_-]{10,}/); // a JWT
  }
});

test("an undefined error does not crash the page", () => {
  expect(typeof loginErrorMessage(undefined)).toBe("string");
  expect(typeof loginErrorMessage(null)).toBe("string");
});
