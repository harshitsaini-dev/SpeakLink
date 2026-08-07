/**
 * The broadcaster's Web Audience panel.
 *
 * The properties worth protecting are honesty ones: the counts stay separate,
 * "Listening" comes from the listener's reported playback state rather than
 * from being connected, and a password that only exists as a hash is described
 * as configured rather than shown as a fake masked value.
 */
import React from "react";
import { render, screen, act, fireEvent, cleanup } from "@testing-library/react";
import WebAudiencePanel, { listenerLink } from "./WebAudiencePanel";

jest.mock("@/lib/api", () => ({
  api: { get: jest.fn(), post: jest.fn(), put: jest.fn() },
}));

// eslint-disable-next-line import/first
const { api } = require("@/lib/api");

const ROOM = {
  public_code: "EC-7K4P92",
  status: "OPEN",
  auto_approve: false,
  delivery: "ok",
  password: "Q7KM-92PX",
  password_configured: true,
  password_rotated_at: null,
  counts: { waiting: 2, admitted: 3, connected: 2, listening: 1,
            buffering: 1, paused: 0 },
  waiting: [
    { id: 11, display_name: "Aman", admission_status: "REQUESTED" },
    { id: 12, display_name: "Vikas", admission_status: "REQUESTED" },
  ],
  listeners: [
    { id: 21, display_name: "Harshit", admission_status: "PASSWORD_ADMITTED",
      admitted_by: "password", playback_state: "LISTENING",
      connected: true, seconds_since_seen: 1, stale: false },
    { id: 22, display_name: "Rohit", admission_status: "APPROVED",
      admitted_by: "approval", playback_state: "BUFFERING",
      connected: true, seconds_since_seen: 4, stale: false },
    { id: 23, display_name: "Harshit", admission_status: "APPROVED",
      admitted_by: "approval", playback_state: "DISCONNECTED",
      connected: false, seconds_since_seen: 40, stale: true },
  ],
};

beforeEach(() => {
  jest.clearAllMocks();
  api.get.mockResolvedValue({ data: ROOM });
  navigator.clipboard = { writeText: jest.fn().mockResolvedValue() };
});

afterEach(cleanup);

async function renderPanel(room = ROOM) {
  api.get.mockResolvedValue({ data: room });
  render(<WebAudiencePanel sessionId={7} />);
  await act(async () => {});
}

// ===========================================================================
// Identity
// ===========================================================================

test("the Broadcast ID and a copyable link are shown", async () => {
  await renderPanel();
  expect(screen.getByTestId("web-room-code").textContent).toContain("EC-7K4P92");

  await act(async () => { fireEvent.click(screen.getByTestId("web-copy-link")); });
  expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
    expect.stringContaining("/listen/EC-7K4P92"));
});

test("the link is built from the current origin, never a hardcoded host", () => {
  // On a LAN pilot the browser is on 192.168.x.x, and a localhost link would
  // work only on the HQ machine itself.
  expect(listenerLink("EC-7K4P92")).toBe(`${window.location.origin}/listen/EC-7K4P92`);
  expect(listenerLink("EC-7K4P92")).not.toContain("localhost:8000");
});

// ===========================================================================
// The password
// ===========================================================================

test("a freshly generated password can be seen and copied", async () => {
  await renderPanel();
  expect(screen.getByTestId("web-room-password").textContent).toContain("Q7KM-92PX");
  await act(async () => { fireEvent.click(screen.getByTestId("web-copy-password")); });
  expect(navigator.clipboard.writeText).toHaveBeenCalledWith("Q7KM-92PX");
});

test("after a refresh the panel says configured rather than showing a fake value", async () => {
  // Only the hash survives. A masked placeholder would imply EchoCast knows
  // the password and is merely hiding it.
  await renderPanel({ ...ROOM, password: null });

  expect(screen.queryByTestId("web-room-password")).toBeNull();
  expect(screen.getByTestId("web-password-configured").textContent)
    .toMatch(/password configured/i);
  expect(screen.getByTestId("web-rotate-password")).toBeTruthy();
});

test("rotating asks the server and shows the new password", async () => {
  await renderPanel({ ...ROOM, password: null });
  api.post.mockResolvedValue({ data: { ...ROOM, password: "NEWP-4RD9" } });

  await act(async () => { fireEvent.click(screen.getByTestId("web-rotate-password")); });
  expect(api.post).toHaveBeenCalledWith(
    "/broadcast/sessions/7/web-room/password/rotate");
  expect(screen.getByTestId("web-room-password").textContent).toContain("NEWP-4RD9");
});

// ===========================================================================
// Auto Approve
// ===========================================================================

test("Auto Approve is off and spells out what turning it on means", async () => {
  await renderPanel();
  const toggle = screen.getByTestId("web-auto-approve");
  expect(toggle.checked).toBe(false);
  // An operator should not have to infer that this opens the door.
  expect(screen.getByTestId("web-audience-panel").textContent)
    .toMatch(/anyone with this Broadcast ID or link/i);
});

test("toggling Auto Approve sends the whole desired state", async () => {
  await renderPanel();
  api.put.mockResolvedValue({ data: { ...ROOM, auto_approve: true } });

  await act(async () => {
    fireEvent.click(screen.getByTestId("web-auto-approve"));
  });
  expect(api.put).toHaveBeenCalledWith(
    "/broadcast/sessions/7/web-room/auto-approve", { auto_approve: true });
});

// ===========================================================================
// Counts and states
// ===========================================================================

test("waiting, connected and listening are three separate numbers", async () => {
  await renderPanel();
  // Collapsing these would let a console claim an audience that hears nothing.
  expect(screen.getByTestId("web-count-waiting").textContent).toContain("2");
  expect(screen.getByTestId("web-count-connected").textContent).toContain("2");
  expect(screen.getByTestId("web-count-listening").textContent).toContain("1");
});

test("each listener's own playback state is shown, not a shared one", async () => {
  await renderPanel();
  expect(screen.getByTestId("web-listener-state-21").textContent).toBe("Listening");
  expect(screen.getByTestId("web-listener-state-22").textContent).toBe("Buffering");
  // Connected is not listening, and admitted-but-absent is neither.
  expect(screen.getByTestId("web-listener-state-23").textContent).toBe("Not connected");
});

test("a stale heartbeat is shown as stale", async () => {
  await renderPanel();
  expect(screen.getByTestId("web-listener-stale-23").textContent).toMatch(/last seen/i);
});

test("two listeners may share a name and remain distinct rows", async () => {
  await renderPanel();
  expect(screen.getByTestId("web-listener-21")).toBeTruthy();
  expect(screen.getByTestId("web-listener-23")).toBeTruthy();
  expect(screen.getAllByText("Harshit").length).toBe(2);
});

test("the panel says plainly what Listening does not prove", async () => {
  await renderPanel();
  expect(screen.getByTestId("web-audience-panel").textContent)
    .toMatch(/can.?t confirm their device volume/i);
});

// ===========================================================================
// Actions
// ===========================================================================

test("Approve and Deny act on the participant id, never the name", async () => {
  await renderPanel();
  api.post.mockResolvedValue({ data: ROOM });

  await act(async () => { fireEvent.click(screen.getByTestId("web-approve-11")); });
  expect(api.post).toHaveBeenCalledWith(
    "/broadcast/sessions/7/web-participants/11/approve");

  await act(async () => { fireEvent.click(screen.getByTestId("web-deny-12")); });
  expect(api.post).toHaveBeenCalledWith(
    "/broadcast/sessions/7/web-participants/12/deny");
});

test("Kick acts on one listener", async () => {
  await renderPanel();
  api.post.mockResolvedValue({ data: { ...ROOM, listeners: [], counts: {} } });

  await act(async () => { fireEvent.click(screen.getByTestId("web-kick-21")); });
  expect(api.post).toHaveBeenCalledWith(
    "/broadcast/sessions/7/web-participants/21/kick");
});

test("a failed action is reported rather than silently swallowed", async () => {
  await renderPanel();
  api.post.mockRejectedValue({ response: { data: { detail: "already denied" } } });

  await act(async () => { fireEvent.click(screen.getByTestId("web-approve-11")); });
  expect(screen.getByTestId("web-audience-error").textContent).toContain("already denied");
});

// ===========================================================================
// Degraded delivery
// ===========================================================================

test("a broken web stream is reported without touching the Stores", async () => {
  await renderPanel({ ...ROOM, delivery: "unavailable" });
  expect(screen.getByTestId("web-delivery-unavailable").textContent)
    .toMatch(/web audience unavailable/i);
});

test("no Store, Zone or Receiver appears in the audience panel", async () => {
  await renderPanel();
  const text = screen.getByTestId("web-audience-panel").textContent;
  // Stores and web listeners are separate delivery classes and stay separate.
  for (const word of ["Store", "Zone", "Receiver", "Volume"]) {
    expect(text).not.toContain(word);
  }
});

test("nothing renders before a session exists", async () => {
  render(<WebAudiencePanel sessionId={null} />);
  await act(async () => {});
  expect(screen.queryByTestId("web-audience-panel")).toBeNull();
  expect(api.get).not.toHaveBeenCalled();
});
