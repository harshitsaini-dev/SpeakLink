/**
 * The public listener page.
 *
 * What matters here is that it never claims more than it knows. "Listening"
 * appears only after a real media event, an autoplay refusal produces a tap
 * prompt rather than a lie, and being kicked or the Broadcast ending stops the
 * page rather than starting a retry loop that can never succeed.
 */
import React from "react";
import { render, screen, act, fireEvent, waitFor, cleanup } from "@testing-library/react";
import Listen from "./Listen";

let mockParams;
jest.mock("react-router-dom", () => ({
  useParams: () => mockParams,
}), { virtual: true });

jest.mock("@/lib/api", () => ({
  api: { get: jest.fn(), post: jest.fn() },
  // Mirrors the real wsUrl, which supplies the /api prefix itself. A mock
  // that echoed the path unchanged hid a doubled /api/api in the real URL.
  wsUrl: (path) => `ws://hq.test/api${path}`,
}));

// eslint-disable-next-line import/first
const { api } = require("@/lib/api");

/** A WebSocket the test drives, standing in for the listener socket. */
class FakeSocket {
  constructor(url) {
    this.url = url;
    this.sent = [];
    this.readyState = 1;
    FakeSocket.last = this;
  }
  send(data) { this.sent.push(data); }
  close() { this.readyState = 3; }
}

let mediaEvents;

function stubAudio() {
  mediaEvents = {};
  const proto = window.HTMLMediaElement.prototype;
  Object.defineProperty(proto, "paused", {
    configurable: true, writable: true, value: true,
  });
  proto.load = jest.fn();
  proto.pause = jest.fn();
  proto.play = jest.fn(function play() { this.paused = false; return Promise.resolve(); });
  // jsdom has no MediaSource at all.
  window.MediaSource = function MediaSourceStub() {
    this.readyState = "closed";
    this.addEventListener = (type, handler) => { mediaEvents[type] = handler; };
    this.removeEventListener = () => {};
    this.addSourceBuffer = () => ({
      mode: "", updating: false, buffered: { length: 0 },
      addEventListener: () => {}, appendBuffer: () => {}, remove: () => {},
    });
    this.endOfStream = () => {};
  };
  window.MediaSource.isTypeSupported = () => true;
  global.URL.createObjectURL = jest.fn(() => "blob:listener");
  global.URL.revokeObjectURL = jest.fn();
}

beforeEach(() => {
  jest.clearAllMocks();
  mockParams = {};
  stubAudio();
  global.WebSocket = FakeSocket;
  global.WebSocket.OPEN = 1;
  FakeSocket.last = null;
  navigator.clipboard = { writeText: jest.fn().mockResolvedValue() };
});

afterEach(cleanup);

async function renderListen() {
  render(<Listen />);
  await act(async () => {});
}

async function fillAndJoin({ name = "Harshit", password = "Q7KM-92PX" } = {}) {
  fireEvent.change(screen.getByTestId("listen-code"), { target: { value: "SL-7K4P92" } });
  fireEvent.change(screen.getByTestId("listen-name"), { target: { value: name } });
  fireEvent.change(screen.getByTestId("listen-password"), { target: { value: password } });
  await act(async () => { fireEvent.click(screen.getByTestId("listen-join")); });
}

// ===========================================================================
// The form
// ===========================================================================

test("a Broadcast ID in the link is filled in for the listener", async () => {
  mockParams = { publicCode: "SL-7K4P92" };
  await renderListen();
  expect(screen.getByTestId("listen-code").value).toBe("SL-7K4P92");
});

test("the page shows no dashboard, no Stores and no volume control", async () => {
  await renderListen();
  // A listener is not an operator with fewer buttons.
  for (const forbidden of [/store/i, /zone/i, /receiver/i, /system log/i,
                           /user management/i, /history/i]) {
    expect(screen.queryByText(forbidden)).toBeNull();
  }
  expect(screen.queryByRole("slider")).toBeNull();
});

test("a name is required before joining", async () => {
  await renderListen();
  fireEvent.change(screen.getByTestId("listen-code"), { target: { value: "SL-7K4P92" } });
  await act(async () => { fireEvent.click(screen.getByTestId("listen-join")); });

  expect(screen.getByTestId("listen-error").textContent).toMatch(/name/i);
  expect(api.post).not.toHaveBeenCalled();
});

// ===========================================================================
// Joining
// ===========================================================================

test("a correct password goes straight to the live screen", async () => {
  api.post.mockResolvedValue({ data: {
    admitted: true, admission_status: "PASSWORD_ADMITTED",
    display_name: "Harshit", broadcast_live: true, public_code: "SL-7K4P92",
  } });
  await renderListen();
  await fillAndJoin();

  expect(screen.getByTestId("listen-live")).toBeTruthy();
  expect(screen.getByTestId("listen-room-code").textContent).toContain("SL-7K4P92");
  expect(screen.getByTestId("listen-display-name").textContent).toContain("Harshit");
  // No credential of any kind in the socket URL.
  expect(FakeSocket.last.url).toBe("ws://hq.test/api/listen/ws");
  expect(FakeSocket.last.url).not.toMatch(/token|jwt|password/i);
});

test("a wrong password says so, and does not become a request", async () => {
  api.post.mockRejectedValue({ response: { status: 401 } });
  await renderListen();
  await fillAndJoin({ password: "WRONG" });

  expect(screen.getByTestId("listen-error").textContent).toMatch(/incorrect password/i);
  expect(screen.queryByTestId("listen-waiting")).toBeNull();
  expect(screen.queryByTestId("listen-live")).toBeNull();
});

test("too many attempts is reported honestly", async () => {
  api.post.mockRejectedValue({ response: { status: 429 } });
  await renderListen();
  await fillAndJoin({ password: "WRONG" });
  expect(screen.getByTestId("listen-error").textContent).toMatch(/too many attempts/i);
});

test("Request Access waits for the broadcaster", async () => {
  api.post.mockResolvedValue({ data: {
    admitted: false, admission_status: "REQUESTED",
    display_name: "Aman", broadcast_live: true,
  } });
  await renderListen();
  fireEvent.change(screen.getByTestId("listen-code"), { target: { value: "SL-7K4P92" } });
  fireEvent.change(screen.getByTestId("listen-name"), { target: { value: "Aman" } });
  await act(async () => { fireEvent.click(screen.getByTestId("listen-request")); });

  expect(screen.getByTestId("listen-waiting").textContent).toMatch(/waiting for broadcaster/i);
  // No audio socket is opened before admission.
  expect(FakeSocket.last).toBeNull();
});

// ===========================================================================
// Playback truth
// ===========================================================================

test("Listening appears only after the browser really starts playing", async () => {
  api.post.mockResolvedValue({ data: {
    admitted: true, admission_status: "PASSWORD_ADMITTED",
    display_name: "Harshit", broadcast_live: true,
  } });
  await renderListen();
  await fillAndJoin();

  // Connected, bootstrapped, play() resolved - and STILL not "Listening",
  // because no playing event has fired.
  await act(async () => {
    FakeSocket.last.onmessage({ data: JSON.stringify({
      type: "bootstrap", heartbeat_seconds: 10 }) });
  });
  expect(screen.getByTestId("listen-status").textContent).not.toContain("Listening");

  await act(async () => {
    screen.getByTestId("listener-audio").dispatchEvent(new Event("playing"));
  });
  expect(screen.getByTestId("listen-status").textContent).toContain("Listening");
});

test("an autoplay refusal asks for a tap and never claims to be listening", async () => {
  window.HTMLMediaElement.prototype.play = jest.fn(() =>
    Promise.reject(new DOMException("blocked", "NotAllowedError")));
  api.post.mockResolvedValue({ data: {
    admitted: true, admission_status: "PASSWORD_ADMITTED",
    display_name: "Harshit", broadcast_live: true,
  } });
  await renderListen();
  await fillAndJoin();
  await act(async () => {
    FakeSocket.last.onmessage({ data: JSON.stringify({ type: "bootstrap" }) });
  });

  expect(screen.getByTestId("listen-tap-to-start")).toBeTruthy();
  // The prompt says "Tap to Start Listening", so the check has to be that the
  // status is not the bare LISTENING state rather than that the word is absent.
  expect(screen.getByTestId("listen-status").textContent.trim())
    .toBe("Tap to Start Listening");
});

test("buffering and paused are shown as themselves", async () => {
  api.post.mockResolvedValue({ data: {
    admitted: true, admission_status: "PASSWORD_ADMITTED",
    display_name: "Harshit", broadcast_live: true,
  } });
  await renderListen();
  await fillAndJoin();
  await act(async () => {
    FakeSocket.last.onmessage({ data: JSON.stringify({ type: "bootstrap" }) });
    screen.getByTestId("listener-audio").dispatchEvent(new Event("playing"));
  });

  await act(async () => {
    screen.getByTestId("listener-audio").dispatchEvent(new Event("waiting"));
  });
  expect(screen.getByTestId("listen-status").textContent).toMatch(/buffering/i);

  await act(async () => {
    screen.getByTestId("listener-audio").dispatchEvent(new Event("pause"));
  });
  expect(screen.getByTestId("listen-status").textContent).toMatch(/paused/i);
});

// ===========================================================================
// Ending
// ===========================================================================

test("being kicked stops the page and says so", async () => {
  api.post.mockResolvedValue({ data: {
    admitted: true, admission_status: "PASSWORD_ADMITTED",
    display_name: "Harshit", broadcast_live: true,
  } });
  await renderListen();
  await fillAndJoin();
  const socket = FakeSocket.last;

  await act(async () => {
    socket.onmessage({ data: JSON.stringify({ type: "kicked" }) });
  });
  expect(screen.getByTestId("listen-kicked").textContent).toMatch(/removed from this Broadcast/i);
  expect(socket.readyState).toBe(3);
});

test("the Broadcast ending stops the page rather than retrying forever", async () => {
  jest.useFakeTimers();
  try {
    api.post.mockResolvedValue({ data: {
      admitted: true, admission_status: "PASSWORD_ADMITTED",
      display_name: "Harshit", broadcast_live: true,
    } });
    render(<Listen />);
    await act(async () => {});
    fireEvent.change(screen.getByTestId("listen-code"), { target: { value: "SL-7K4P92" } });
    fireEvent.change(screen.getByTestId("listen-name"), { target: { value: "Harshit" } });
    await act(async () => { fireEvent.click(screen.getByTestId("listen-join")); });

    const first = FakeSocket.last;
    await act(async () => {
      first.onmessage({ data: JSON.stringify({ type: "room_ended" }) });
    });
    expect(screen.getByTestId("listen-ended").textContent).toMatch(/broadcast ended/i);

    // A minute of timers later, still no reconnect attempt.
    await act(async () => { jest.advanceTimersByTime(60_000); });
    expect(FakeSocket.last).toBe(first);
  } finally {
    jest.useRealTimers();
  }
});

test("a refused session closes the page instead of looping", async () => {
  jest.useFakeTimers();
  try {
    api.post.mockResolvedValue({ data: {
      admitted: true, admission_status: "PASSWORD_ADMITTED",
      display_name: "Harshit", broadcast_live: true,
    } });
    render(<Listen />);
    await act(async () => {});
    fireEvent.change(screen.getByTestId("listen-code"), { target: { value: "SL-7K4P92" } });
    fireEvent.change(screen.getByTestId("listen-name"), { target: { value: "Harshit" } });
    await act(async () => { fireEvent.click(screen.getByTestId("listen-join")); });

    const first = FakeSocket.last;
    // 4401 means this browser has no valid listener session. Retrying can
    // never succeed - but it is NOT the Broadcast ending, and saying so to
    // somebody who was just approved is the defect this distinction fixes.
    await act(async () => { first.onclose({ code: 4401 }); });
    await act(async () => { jest.advanceTimersByTime(60_000); });

    expect(screen.getByTestId("listen-session-lost")).toBeTruthy();
    expect(screen.queryByTestId("listen-ended")).toBeNull();
    expect(FakeSocket.last).toBe(first);
  } finally {
    jest.useRealTimers();
  }
});

test("an ordinary disconnect reconnects with backoff", async () => {
  jest.useFakeTimers();
  try {
    api.post.mockResolvedValue({ data: {
      admitted: true, admission_status: "PASSWORD_ADMITTED",
      display_name: "Harshit", broadcast_live: true,
    } });
    render(<Listen />);
    await act(async () => {});
    fireEvent.change(screen.getByTestId("listen-code"), { target: { value: "SL-7K4P92" } });
    fireEvent.change(screen.getByTestId("listen-name"), { target: { value: "Harshit" } });
    await act(async () => { fireEvent.click(screen.getByTestId("listen-join")); });

    const first = FakeSocket.last;
    await act(async () => { first.onclose({ code: 1006 }); });
    // Not immediately: a hundred listeners must not all return at once.
    expect(FakeSocket.last).toBe(first);

    await act(async () => { jest.advanceTimersByTime(3000); });
    expect(FakeSocket.last).not.toBe(first);
  } finally {
    jest.useRealTimers();
  }
});


// ===========================================================================
// The refusal and progress defects found by manual LAN testing
// ===========================================================================

test("a refused socket reports the refusal instead of retrying for ever", async () => {
  jest.useFakeTimers();
  try {
    api.post.mockResolvedValue({ data: {
      admitted: true, admission_status: "PASSWORD_ADMITTED",
      display_name: "Harshit", broadcast_live: true,
    } });
    render(<Listen />);
    await act(async () => {});
    fireEvent.change(screen.getByTestId("listen-code"), { target: { value: "SL-7K4P92" } });
    fireEvent.change(screen.getByTestId("listen-name"), { target: { value: "Harshit" } });
    await act(async () => { fireEvent.click(screen.getByTestId("listen-join")); });

    const first = FakeSocket.last;
    // The server now ACCEPTS before refusing, so the reason arrives as a
    // message. Closing before the handshake completes only ever reaches a
    // browser as 1006, which is why a refusal used to look like a dropped
    // network and buffer for ever.
    await act(async () => {
      first.onmessage({ data: JSON.stringify({ type: "refused", reason: "not_admitted" }) });
    });
    expect(screen.getByTestId("listen-session-lost")).toBeTruthy();

    await act(async () => { jest.advanceTimersByTime(60_000); });
    expect(FakeSocket.last).toBe(first);
  } finally {
    jest.useRealTimers();
  }
});

test("being admitted before the Broadcast starts is waiting, not ended", async () => {
  api.post.mockResolvedValue({ data: {
    admitted: true, admission_status: "PASSWORD_ADMITTED",
    display_name: "Harshit", broadcast_live: false,
  } });
  await renderListen();
  await fillAndJoin();

  await act(async () => {
    FakeSocket.last.onmessage({ data: JSON.stringify({
      type: "refused", reason: "not_started" }) });
  });
  expect(screen.getByTestId("listen-not-started-yet")).toBeTruthy();
  expect(screen.queryByTestId("listen-ended")).toBeNull();
});

test("a listener whose session is rejected is not told the Broadcast ended", async () => {
  // The exact manual defect: Request Access, get approved, and the poll comes
  // back 401 because the browser refused the cookie.
  api.post.mockResolvedValue({ data: {
    admitted: false, admission_status: "REQUESTED",
    display_name: "Aman", broadcast_live: true,
  } });
  api.get.mockRejectedValue({ response: { status: 401 } });

  await renderListen();
  fireEvent.change(screen.getByTestId("listen-code"), { target: { value: "SL-7K4P92" } });
  fireEvent.change(screen.getByTestId("listen-name"), { target: { value: "Aman" } });
  await act(async () => { fireEvent.click(screen.getByTestId("listen-request")); });
  expect(screen.getByTestId("listen-waiting")).toBeTruthy();

  await act(async () => { await new Promise((r) => setTimeout(r, 2000)); });
  expect(screen.queryByTestId("listen-ended")).toBeNull();
  expect(screen.getByTestId("listen-session-lost")).toBeTruthy();
});

test("the listener's own page carries the theme control, and follows it", () => {
  // The link arrives at ten at night and this is the first screen. A toggle
  // that only existed inside HQ would be a setting for the people who never
  // see this page - and a toggle that did not change THIS page would be a
  // control that does nothing where it stands.
  // Wrapped, because a theme choice is only real when something is holding
  // it: outside the provider the control renders and does nothing, which is
  // exactly the case the fallback in useTheme exists to survive.
  const { ThemeProvider } = require("@/contexts/ThemeContext");
  render(<ThemeProvider><Listen /></ThemeProvider>);
  expect(screen.getByTestId("theme-toggle")).toBeTruthy();

  const shell = document.querySelector(".listener-shell");
  expect(shell).toBeTruthy();

  fireEvent.click(screen.getByTestId("theme-light"));
  expect(document.documentElement.classList.contains("dark")).toBe(false);
  expect(shell.className).not.toMatch(/\bnight\b/);

  fireEvent.click(screen.getByTestId("theme-dark"));
  expect(document.documentElement.classList.contains("dark")).toBe(true);
  expect(document.querySelector(".listener-shell").className).toMatch(/\bnight\b/);
});
