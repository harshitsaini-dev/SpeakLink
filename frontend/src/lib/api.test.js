/**
 * The backend address must come from the browser, not from the build machine.
 *
 * RC4 reached READY, the auto-start verifier passed 34 checks, the backend
 * answered on http://192.168.4.134:8000 and the frontend served on :3000 - and
 * signing in was impossible. The bundle had been built with
 *
 *     REACT_APP_BACKEND_URL=http://127.0.0.1:8000
 *
 * baked in at compile time, so a browser on any other computer sent its login to
 * its OWN loopback address and got ERR_CONNECTION_REFUSED. The request never
 * reached authentication at all.
 *
 * Two separate defects, and the second is what made the first expensive:
 *
 * 1. A build-time constant described a runtime fact. The address of the HQ
 *    machine is not knowable when the bundle is compiled.
 * 2. The login page reported "Login failed" for a transport failure. An operator
 *    reasonably reads that as "wrong password" and starts resetting credentials -
 *    on a system where a password reset is a deliberate, audited act.
 *
 * These tests pin the resolver's behaviour at the level the defect lives: what
 * URL is produced for a given browser location.
 */

const ORIGINAL_ENV = process.env.REACT_APP_BACKEND_URL;

/** Reload the module with a chosen browser location and environment. */
function loadApi({ href, hostname, protocol, port, backendUrl }) {
  jest.resetModules();

  if (backendUrl === undefined) {
    delete process.env.REACT_APP_BACKEND_URL;
  } else {
    process.env.REACT_APP_BACKEND_URL = backendUrl;
  }

  // The href has to be a URL jsdom will accept, which means bracketing an IPv6
  // literal here too - `http://::1:3000/login` throws "Invalid URL". That is the
  // same trap the resolver exists to avoid, met one level up while writing the
  // test for it. `hostname` stays unbracketed, because that is exactly what a
  // real `window.location.hostname` returns.
  const hostForHref = hostname.includes(":") && !hostname.startsWith("[")
    ? `[${hostname}]`
    : hostname;

  delete window.location;
  window.location = {
    href: href || `${protocol}//${hostForHref}${port ? `:${port}` : ""}/login`,
    hostname,
    protocol,
    port: port || "",
    host: `${hostForHref}${port ? `:${port}` : ""}`,
    pathname: "/login",
  };

  // eslint-disable-next-line global-require
  return require("./api");
}

afterEach(() => {
  if (ORIGINAL_ENV === undefined) {
    delete process.env.REACT_APP_BACKEND_URL;
  } else {
    process.env.REACT_APP_BACKEND_URL = ORIGINAL_ENV;
  }
  jest.resetModules();
});

// ===========================================================================
// 1. The failure from the installed machine
// ===========================================================================
test("a page served from the HQ LAN address calls that same address", () => {
  const api = loadApi({ hostname: "192.168.4.134", protocol: "http:", port: "3000" });

  expect(api.API_BASE).toBe("http://192.168.4.134:8000/api");
});

test("the resolved base never points at loopback when the page does not", () => {
  const api = loadApi({ hostname: "192.168.4.134", protocol: "http:", port: "3000" });

  expect(api.API_BASE).not.toContain("127.0.0.1");
  expect(api.API_BASE).not.toContain("localhost");
});

test("any other Store or HQ machine resolves to itself, with no rebuild", () => {
  // The same bundle, opened from three different addresses. A build-time
  // constant cannot do this, which is the whole point.
  for (const host of ["10.0.0.5", "192.168.1.77", "hq-desk.local"]) {
    const api = loadApi({ hostname: host, protocol: "http:", port: "3000" });
    expect(api.API_BASE).toBe(`http://${host}:8000/api`);
  }
});

// ===========================================================================
// 2. Local development keeps working
// ===========================================================================
test("a page on localhost resolves to localhost", () => {
  const api = loadApi({ hostname: "localhost", protocol: "http:", port: "3000" });

  expect(api.API_BASE).toBe("http://localhost:8000/api");
});

test("a page on 127.0.0.1 resolves to 127.0.0.1", () => {
  const api = loadApi({ hostname: "127.0.0.1", protocol: "http:", port: "3000" });

  expect(api.API_BASE).toBe("http://127.0.0.1:8000/api");
});

// ===========================================================================
// 3. The explicit override is preserved, for controlled builds
// ===========================================================================
test("an explicit REACT_APP_BACKEND_URL still wins", () => {
  const api = loadApi({
    hostname: "192.168.4.134", protocol: "http:", port: "3000",
    backendUrl: "http://backend.example.internal:9000",
  });

  expect(api.API_BASE).toBe("http://backend.example.internal:9000/api");
});

test("a trailing slash on the override does not produce a double slash", () => {
  const api = loadApi({
    hostname: "192.168.4.134", protocol: "http:", port: "3000",
    backendUrl: "http://backend.example.internal:9000/",
  });

  expect(api.API_BASE).toBe("http://backend.example.internal:9000/api");
});

test("a blank override is ignored rather than producing undefined/api", () => {
  // The old code produced the literal string "undefined/api" when the variable
  // was missing. Blank must fall back, not poison every URL in the app.
  for (const blank of ["", "   "]) {
    const api = loadApi({
      hostname: "192.168.4.134", protocol: "http:", port: "3000", backendUrl: blank,
    });
    expect(api.API_BASE).toBe("http://192.168.4.134:8000/api");
  }
});

test("a loopback override is refused in a production build", () => {
  // This is the exact value that shipped. Honouring it in a production bundle
  // would reintroduce the defect through configuration instead of through code.
  const api = loadApi({
    hostname: "192.168.4.134", protocol: "http:", port: "3000",
    backendUrl: "http://127.0.0.1:8000",
  });

  expect(api.API_BASE).toBe("http://192.168.4.134:8000/api");
});

test("a loopback override is honoured when the page itself is loopback", () => {
  // A developer running both halves on one machine is the case the override was
  // written for, and it must keep working.
  const api = loadApi({
    hostname: "localhost", protocol: "http:", port: "3000",
    backendUrl: "http://127.0.0.1:8000",
  });

  expect(api.API_BASE).toBe("http://127.0.0.1:8000/api");
});

// ===========================================================================
// 4. WebSockets follow the same resolution
// ===========================================================================
test("an http page produces a ws:// socket on the same host", () => {
  const api = loadApi({ hostname: "192.168.4.134", protocol: "http:", port: "3000" });

  expect(api.wsUrl("/ws/hq")).toBe("ws://192.168.4.134:8000/api/ws/hq");
});

test("an https page produces a wss:// socket, not ws://", () => {
  const api = loadApi({ hostname: "hq.example.com", protocol: "https:", port: "" });

  expect(api.wsUrl("/ws/hq")).toBe("wss://hq.example.com:8000/api/ws/hq");
});

test("the socket host always matches the API host", () => {
  const api = loadApi({ hostname: "192.168.4.134", protocol: "http:", port: "3000" });

  const apiHost = new URL(api.API_BASE).host;
  const socketHost = new URL(api.wsUrl("/ws/receiver/abc").replace(/^ws/, "http")).host;
  expect(socketHost).toBe(apiHost);
});

test("an explicit https override produces wss", () => {
  const api = loadApi({
    hostname: "hq.example.com", protocol: "https:", port: "",
    backendUrl: "https://backend.example.com:8443",
  });

  expect(api.wsUrl("/ws/hq")).toBe("wss://backend.example.com:8443/api/ws/hq");
});

// ===========================================================================
// 5. IPv6, because a bare literal in a URL is a parse error
// ===========================================================================
test("an IPv6 host is bracketed", () => {
  // window.location.hostname strips the brackets, so re-adding them is the
  // caller's job. "http://::1:8000" is not a URL.
  const api = loadApi({ hostname: "::1", protocol: "http:", port: "3000" });

  expect(api.API_BASE).toBe("http://[::1]:8000/api");
  expect(() => new URL(api.API_BASE)).not.toThrow();
});

test("an already-bracketed IPv6 host is not double-bracketed", () => {
  const api = loadApi({ hostname: "[::1]", protocol: "http:", port: "3000" });

  expect(api.API_BASE).toBe("http://[::1]:8000/api");
});

test("a full IPv6 address is bracketed and parses", () => {
  const api = loadApi({ hostname: "fd00::1234:5678", protocol: "http:", port: "3000" });

  expect(api.API_BASE).toBe("http://[fd00::1234:5678]:8000/api");

  // The two APIs disagree, and that disagreement IS the bug being guarded
  // against: `window.location.hostname` yields an IPv6 address WITHOUT brackets,
  // while WHATWG `URL.hostname` yields it WITH them. Anything that moves a host
  // from one to the other has to add the brackets, which is what the resolver
  // does.
  const parsed = new URL(api.API_BASE);
  expect(parsed.hostname).toBe("[fd00::1234:5678]");
  expect(parsed.port).toBe("8000");
});

// ===========================================================================
// 6. Nothing else about the client changed
// ===========================================================================
test("the token helpers are unchanged", () => {
  const api = loadApi({ hostname: "192.168.4.134", protocol: "http:", port: "3000" });

  api.setToken("a-test-token");
  expect(api.getToken()).toBe("a-test-token");
  api.clearToken();
  expect(api.getToken()).toBeNull();
});

test("the axios instance is built with the resolved base", () => {
  const api = loadApi({ hostname: "192.168.4.134", protocol: "http:", port: "3000" });

  expect(api.api.defaults.baseURL).toBe("http://192.168.4.134:8000/api");
});

test("no module-level code writes a URL into storage or the document", () => {
  const api = loadApi({ hostname: "192.168.4.134", protocol: "http:", port: "3000" });

  expect(localStorage.getItem("speaklink_token")).toBeNull();
  expect(api.API_BASE).toBeDefined();
});

// ===========================================================================
// 7. The port is configurable without hard-coding an address
// ===========================================================================
test("the backend port can be configured without naming a host", () => {
  const api = loadApi({
    hostname: "192.168.4.134", protocol: "http:", port: "3000",
  });
  // Default remains 8000, which is what every script and document in this
  // repository already says.
  expect(api.API_BASE).toBe("http://192.168.4.134:8000/api");
  expect(api.BACKEND_PORT).toBe("8000");
});

// ===========================================================================
// Same-origin repo-native mode
// ===========================================================================
// One Uvicorn worker serves /api, the WebSocket routes and the built React
// app on ONE origin. Every request is then relative, which is why repo-native
// production needs no CORS at all - and why the same bundle works unchanged
// from any hostname.
test("a page served by the API itself uses relative URLs", () => {
  const api = loadApi({ hostname: "192.168.4.134", protocol: "http:", port: "8000" });

  expect(api.BACKEND_URL).toBe("");
  expect(api.API_BASE).toBe("/api");
});

test("same-origin works from any hostname without rebuilding", () => {
  for (const hostname of ["192.168.1.50", "hq.internal", "speaklink.example.com"]) {
    const api = loadApi({ hostname, protocol: "http:", port: "8000" });
    expect(api.API_BASE).toBe("/api");
  }
});

test("the same-origin socket is built from the page's own host", () => {
  const api = loadApi({ hostname: "hq.internal", protocol: "http:", port: "8000" });
  expect(api.wsUrl("/ws/hq")).toBe("ws://hq.internal:8000/api/ws/hq");
});

test("an https same-origin page gets a wss socket", () => {
  const api = loadApi({ hostname: "hq.example.com", protocol: "https:", port: "8000" });
  expect(api.wsUrl("/ws/hq")).toBe("wss://hq.example.com:8000/api/ws/hq");
});

test("the legacy two-port layout still names the API origin explicitly", () => {
  // The CRA dev server, and the legacy HQ where a static server served the
  // build on 3000 while the API answered on 8000. This must not regress -
  // it is the rollback path while repo-native mode is being proven.
  const api = loadApi({ hostname: "192.168.4.134", protocol: "http:", port: "3000" });

  expect(api.BACKEND_URL).toBe("http://192.168.4.134:8000");
  expect(api.API_BASE).toBe("http://192.168.4.134:8000/api");
  expect(api.wsUrl("/ws/hq")).toBe("ws://192.168.4.134:8000/api/ws/hq");
});

test("a static host on the default port still reaches the separate API", () => {
  // port "" is not the API port, so this is NOT treated as same-origin.
  const api = loadApi({ hostname: "hq.example.com", protocol: "https:", port: "" });
  expect(api.BACKEND_URL).toBe("https://hq.example.com:8000");
});

test("same-origin names the real host in a cannot-reach message", () => {
  const api = loadApi({ hostname: "hq.internal", protocol: "http:", port: "8000" });
  // Never an empty string, which would render "cannot reach ".
  expect(api.backendDisplayHost()).toBe("hq.internal:8000");
});

test("an explicit override still wins over same-origin", () => {
  const api = loadApi({
    hostname: "hq.internal", protocol: "http:", port: "8000",
    backendUrl: "http://api.internal:9000",
  });
  expect(api.BACKEND_URL).toBe("http://api.internal:9000");
});

test("a request that brings its own Authorization keeps it", async () => {
  const { api, setToken } = require("./api");
  // The listening pages have their own room token and no HQ account. When an
  // operator opens a listening link in the browser they administer from, the
  // HQ token used to overwrite the room token and the room - correctly - did
  // not recognise the operator.
  setToken("hq-session-token");
  const handler = api.interceptors.request.handlers[0].fulfilled;

  const listener = handler({ headers: { Authorization: "Bearer room-token" } });
  expect(listener.headers.Authorization).toBe("Bearer room-token");

  // And an ordinary admin request still gets the session token.
  const admin = handler({ headers: {} });
  expect(admin.headers.Authorization).toBe("Bearer hq-session-token");
});
