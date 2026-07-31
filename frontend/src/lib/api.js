import axios from "axios";

/**
 * WHERE THE BACKEND IS, DECIDED BY THE BROWSER RATHER THAN BY THE BUILD.
 *
 * This used to be one line:
 *
 *     const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
 *
 * which is a build-time constant describing a runtime fact. The HQ machine's
 * address is not knowable when the bundle is compiled, and the value that
 * shipped pointed at loopback on the backend port. RC4 came up healthy - Runtime
 * READY, backend answering on the LAN address, 34 auto-start checks green - and
 * nobody could sign in, because every browser sent its login to its OWN loopback
 * address and got ERR_CONNECTION_REFUSED. The request never reached
 * authentication.
 *
 * (The exact loopback URL is deliberately not written out anywhere that reaches
 * a shipped file, including the source map: a package that contains the string
 * is indistinguishable, to a grep, from one that still calls it.)
 *
 * The page was fetched from the HQ machine, so the browser already knows the
 * address that worked: `window.location.hostname`. That is the answer, and it is
 * the only one that survives the same bundle being opened from 44 different
 * computers.
 *
 * The explicit override is kept, because a controlled deployment may genuinely
 * put the API on a different host or port - but it is ignored when it names a
 * loopback address and the page did not come from one, because that combination
 * has exactly one meaning: a development value that escaped into a production
 * build. Honouring it would be the same defect wearing a different hat.
 */

//: The port the backend listens on. Every script and document in this repository
//: says 8000; it is named here once so nothing has to embed a whole URL.
export const BACKEND_PORT = (process.env.REACT_APP_BACKEND_PORT || "8000").trim();

const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"]);

function isLoopback(hostname) {
  return LOOPBACK_HOSTS.has(String(hostname || "").toLowerCase());
}

/**
 * Wrap an IPv6 literal in brackets.
 *
 * `window.location.hostname` returns an IPv6 address WITHOUT its brackets, and
 * `http://::1:8000` is not a URL - it does not parse and it does not connect.
 * A hostname containing a colon can only be IPv6, because a DNS name cannot.
 */
function bracketIfIpv6(hostname) {
  const host = String(hostname || "");
  if (!host.includes(":")) return host;
  if (host.startsWith("[") && host.endsWith("]")) return host;
  return `[${host}]`;
}

function currentLocation() {
  // Guarded so importing this module cannot throw where there is no DOM - a
  // server-side render or a bare unit test should degrade, not crash.
  if (typeof window === "undefined" || !window.location) {
    return { hostname: "localhost", protocol: "http:" };
  }
  return window.location;
}

/**
 * The backend origin, with no trailing slash.
 *
 * Exported so the resolution can be tested directly rather than inferred from
 * whatever `API_BASE` happens to be.
 */
export function resolveBackendUrl(location = currentLocation()) {
  const configured = String(process.env.REACT_APP_BACKEND_URL || "").trim();
  const pageIsLoopback = isLoopback(location.hostname);

  if (configured) {
    const candidate = configured.replace(/\/+$/, "");
    let hostname = null;
    let parsed = true;
    try {
      hostname = new URL(candidate).hostname;
    } catch (error) {
      // An unparseable override is worse than none: it would make every request
      // in the app fail in a way that looks like a server fault.
      parsed = false;
    }
    if (parsed && !(isLoopback(hostname) && !pageIsLoopback)) {
      return candidate;
    }
  }

  // Same scheme as the page. An https page loading an http API is blocked by the
  // browser as mixed content, which presents as a silent network failure.
  const protocol = location.protocol === "https:" ? "https:" : "http:";
  const host = bracketIfIpv6(location.hostname) || "localhost";
  return `${protocol}//${host}:${BACKEND_PORT}`;
}

export const BACKEND_URL = resolveBackendUrl();
export const API_BASE = `${BACKEND_URL}/api`;

const TOKEN_KEY = "speaklink_token";

export const getToken = () => localStorage.getItem(TOKEN_KEY);
export const setToken = (t) => localStorage.setItem(TOKEN_KEY, t);
export const clearToken = () => localStorage.removeItem(TOKEN_KEY);

export const api = axios.create({ baseURL: API_BASE });

api.interceptors.request.use((config) => {
  const t = getToken();
  if (t) config.headers.Authorization = `Bearer ${t}`;
  return config;
});

api.interceptors.response.use(
  (r) => r,
  (err) => {
    if (err?.response?.status === 401) {
      clearToken();
      if (!window.location.pathname.startsWith("/login") && !window.location.pathname.startsWith("/receiver")) {
        window.location.href = "/login";
      }
    }
    return Promise.reject(err);
  }
);

/**
 * Did this request fail before the server ever answered?
 *
 * `err.response` exists only when a response came back. No response means the
 * request never arrived: connection refused, DNS failure, backend down, wrong
 * address. That is a completely different fact from "the server considered your
 * credentials and said no", and reporting the wrong one costs an operator a
 * password reset they did not need - which in this system is a deliberate,
 * audited act.
 */
export function isNetworkError(err) {
  if (!err) return false;
  if (err.response) return false;
  return Boolean(err.request) || err.code === "ERR_NETWORK" || err.message === "Network Error";
}

/** The backend host to name in a "cannot reach the server" message. */
export function backendDisplayHost() {
  try {
    return new URL(BACKEND_URL).host;
  } catch (error) {
    return BACKEND_URL;
  }
}

export function wsUrl(path) {
  // http -> ws and https -> wss, taken from the resolved backend origin rather
  // than from the page, so an override governs both or neither.
  const base = BACKEND_URL.replace(/^http/, "ws");
  return `${base}/api${path}`;
}
