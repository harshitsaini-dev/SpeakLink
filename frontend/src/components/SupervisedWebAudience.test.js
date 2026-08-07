/**
 * The supervisor's Web Audience panel.
 *
 * Everything it offers comes from the server's capability flags. A control that
 * appeared because the frontend guessed a role would eventually appear for
 * somebody the backend refuses - and the refusal is the one that matters. So
 * these tests drive the flags, not roles.
 */
import React from "react";
import { render, screen, act, fireEvent, cleanup } from "@testing-library/react";
import SupervisedWebAudience from "./SupervisedWebAudience";

jest.mock("@/lib/api", () => ({
  api: { get: jest.fn(), post: jest.fn(), put: jest.fn() },
}));

// eslint-disable-next-line import/first
const { api } = require("@/lib/api");

const FULL_CAPS = {
  can_view_room_credentials: true, can_manage_web_audience: true,
  can_approve: true, can_deny: true, can_kick: true,
  can_toggle_auto_approve: true, can_rotate_password: false,
};

function panel(overrides = {}) {
  return {
    session_id: 7, campaign_name: "Diwali Offer", is_mine: false,
    target_store_count: 0, status: "OPEN", auto_approve: false, delivery: "ok",
    public_code: "EC-K7Q92A", password: "Q7KM-92PX", password_available: true,
    counts: { waiting: 1, admitted: 2, connected: 2, listening: 1,
              buffering: 1, paused: 0 },
    waiting: [{ id: 11, display_name: "Aman", admission_status: "REQUESTED",
                requested_at: "10:32:12" }],
    listeners: [
      { id: 21, display_name: "Harshit", admitted_by: "password",
        playback_state: "LISTENING", connected: true,
        seconds_since_seen: 1, stale: false },
      { id: 22, display_name: "Rohit", admitted_by: "approval",
        playback_state: "BUFFERING", connected: true,
        seconds_since_seen: 40, stale: true },
    ],
    capabilities: FULL_CAPS,
    ...overrides,
  };
}

beforeEach(() => {
  jest.clearAllMocks();
  navigator.clipboard = { writeText: jest.fn().mockResolvedValue() };
});

afterEach(cleanup);

async function show(data = panel()) {
  api.get.mockResolvedValue({ data });
  render(<SupervisedWebAudience sessionId={7} campaignName="Diwali Offer"
                                onClose={() => {}} />);
  await act(async () => {});
}

// ===========================================================================
// Credentials follow the server, never a guess
// ===========================================================================

test("the room code and link appear when the server sent them", async () => {
  await show();
  expect(screen.getByTestId("supervised-room-code").textContent).toContain("EC-K7Q92A");

  await act(async () => { fireEvent.click(screen.getByTestId("supervised-copy-link")); });
  expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
    expect.stringContaining("/listen/EC-K7Q92A"));
  // Built from the current origin, so a LAN pilot gets a LAN link.
  expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
    `${window.location.origin}/listen/EC-K7Q92A`);
  expect(navigator.clipboard.writeText).not.toHaveBeenCalledWith(
    expect.stringContaining("localhost:8000"));
});

test("no room credentials are shown when the server withheld them", async () => {
  // A manager without view_ownership: may act, may not read the credential.
  await show(panel({
    public_code: undefined, password: undefined,
    capabilities: { ...FULL_CAPS, can_view_room_credentials: false },
  }));
  expect(screen.queryByTestId("supervised-room-credentials")).toBeNull();
  expect(screen.queryByTestId("supervised-room-code")).toBeNull();
  // The panel still works for what it IS allowed to do.
  expect(screen.getByTestId("supervised-kick-21")).toBeTruthy();
});

test("an unavailable password is described truthfully, never as asterisks", async () => {
  await show(panel({ password: null, password_available: false }));
  expect(screen.queryByTestId("supervised-room-password")).toBeNull();
  const notice = screen.getByTestId("supervised-password-unavailable");
  expect(notice.textContent).toMatch(/not recoverable/i);
  // EchoCast stores only a hash; printing dots would imply it knows the value.
  expect(notice.textContent).not.toContain("****");
});

test("a password the server supplied can be copied", async () => {
  await show();
  await act(async () => {
    fireEvent.click(screen.getByTestId("supervised-copy-password"));
  });
  expect(navigator.clipboard.writeText).toHaveBeenCalledWith("Q7KM-92PX");
});

// ===========================================================================
// Counts and states
// ===========================================================================

test("waiting, connected and listening stay three separate numbers", async () => {
  await show();
  expect(screen.getByTestId("supervised-count-waiting").textContent).toContain("1");
  expect(screen.getByTestId("supervised-count-connected").textContent).toContain("2");
  expect(screen.getByTestId("supervised-count-listening").textContent).toContain("1");
});

test("each listener's own playback state is shown", async () => {
  await show();
  expect(screen.getByTestId("supervised-state-21").textContent).toBe("Listening");
  expect(screen.getByTestId("supervised-state-22").textContent).toBe("Buffering");
  expect(screen.getByTestId("supervised-stale-22")).toBeTruthy();
});

test("a disconnected listener is not shown as listening", async () => {
  await show(panel({ listeners: [{
    id: 21, display_name: "Harshit", admitted_by: "password",
    playback_state: "DISCONNECTED", connected: false,
    seconds_since_seen: 90, stale: true }] }));
  expect(screen.getByTestId("supervised-state-21").textContent).toBe("Not connected");
});

// ===========================================================================
// Actions, driven by capability flags
// ===========================================================================

test("Approve and Deny act on the participant id", async () => {
  await show();
  api.post.mockResolvedValue({ data: {} });

  await act(async () => { fireEvent.click(screen.getByTestId("supervised-approve-11")); });
  expect(api.post).toHaveBeenCalledWith(
    "/broadcast/active-management/7/web-audience/11/approve");

  await act(async () => { fireEvent.click(screen.getByTestId("supervised-deny-11")); });
  expect(api.post).toHaveBeenCalledWith(
    "/broadcast/active-management/7/web-audience/11/deny");
});

test("Kick acts on one listener", async () => {
  await show();
  api.post.mockResolvedValue({ data: {} });
  await act(async () => { fireEvent.click(screen.getByTestId("supervised-kick-21")); });
  expect(api.post).toHaveBeenCalledWith(
    "/broadcast/active-management/7/web-audience/21/kick");
});

test("no management controls appear when the server refused them", async () => {
  // view_ownership without manage: may read the room, may not touch anybody.
  await show(panel({ capabilities: {
    can_view_room_credentials: true, can_manage_web_audience: false,
    can_approve: false, can_deny: false, can_kick: false,
    can_toggle_auto_approve: false, can_rotate_password: false,
  } }));

  expect(screen.getByTestId("supervised-room-code")).toBeTruthy();
  expect(screen.queryByTestId("supervised-kick-21")).toBeNull();
  expect(screen.queryByTestId("supervised-approve-11")).toBeNull();
  expect(screen.queryByTestId("supervised-deny-11")).toBeNull();
  expect(screen.queryByTestId("supervised-auto-approve")).toBeNull();
});

test("a refused action is reported rather than swallowed", async () => {
  await show();
  api.post.mockRejectedValue({ response: { data: {
    detail: "You do not have permission to manage another operator's web audience." } } });

  await act(async () => { fireEvent.click(screen.getByTestId("supervised-kick-21")); });
  expect(screen.getByTestId("supervised-audience-error").textContent)
    .toMatch(/do not have permission/i);
});

test("a 403 on open is explained", async () => {
  api.get.mockRejectedValue({ response: { status: 403 } });
  render(<SupervisedWebAudience sessionId={7} campaignName="x" onClose={() => {}} />);
  await act(async () => {});
  expect(screen.getByTestId("supervised-audience-error").textContent)
    .toMatch(/do not have permission/i);
});

// ===========================================================================
// Polling
// ===========================================================================

test("the panel polls while open and stops when closed", async () => {
  jest.useFakeTimers();
  try {
    api.get.mockResolvedValue({ data: panel() });
    const view = render(<SupervisedWebAudience sessionId={7} campaignName="x"
                                               onClose={() => {}} />);
    await act(async () => {});
    const initial = api.get.mock.calls.length;

    await act(async () => { jest.advanceTimersByTime(9000); });
    expect(api.get.mock.calls.length).toBeGreaterThan(initial);

    const afterPolling = api.get.mock.calls.length;
    view.unmount();
    // A timer that outlived its panel would keep asking about a Broadcast
    // nobody is looking at.
    await act(async () => { jest.advanceTimersByTime(30_000); });
    expect(api.get.mock.calls.length).toBe(afterPolling);
  } finally {
    jest.useRealTimers();
  }
});

test("no Store, Zone or Receiver appears in the audience panel", async () => {
  await show();
  const text = screen.getByTestId("supervised-audience-modal").textContent;
  for (const word of ["Store", "Zone", "Receiver"]) {
    expect(text).not.toContain(word);
  }
});

test("the panel says what Listening does not prove", async () => {
  await show();
  expect(screen.getByTestId("supervised-audience-modal").textContent)
    .toMatch(/can.?t confirm their device volume/i);
});
