/**
 * Per-Store output control: coalescing, independence, and stale answers.
 *
 * These drive the hook directly rather than the Console, because what needs
 * proving is traffic shape and ordering - how many requests a drag produces,
 * and which answer wins when two race. Rendering a table would obscure both.
 */
import React from "react";
import { render, act, cleanup } from "@testing-library/react";
import { useStoreAudioControl, SEND_INTERVAL_MS } from "./useStoreAudioControl";

jest.mock("@/lib/api", () => ({
  api: { get: jest.fn(), post: jest.fn() },
}));

// eslint-disable-next-line import/first
const { api } = require("@/lib/api");

let hook;

function Harness({ sessionId = 5, canControl = true }) {
  hook = useStoreAudioControl({ sessionId, canControl });
  return null;
}

function row(storeId, overrides = {}) {
  return {
    store_id: storeId,
    requested_volume_percent: 100,
    requested_muted: false,
    applied_volume_percent: null,
    applied_muted: null,
    last_command_id: 1,
    last_acknowledged_command_id: 0,
    result: null,
    pending: true,
    supported: true,
    online: true,
    ...overrides,
  };
}

async function mount(props) {
  await act(async () => { render(<Harness {...props} />); });
}

// The real endpoint returns the WHOLE session's state, with the commanded
// Store updated and a fresh command id. Mocking a stale echo instead would
// make these tests pass against a server that ignores the request.
const serverState = {};

function serverEcho(body) {
  const current = serverState[body.store_id] || {
    requested_volume_percent: 100, requested_muted: false, last_command_id: 0,
  };
  serverState[body.store_id] = {
    requested_volume_percent: "volume_percent" in body
      ? body.volume_percent : current.requested_volume_percent,
    requested_muted: "muted" in body ? body.muted : current.requested_muted,
    last_command_id: current.last_command_id + 1,
  };
  return {
    data: {
      session_id: 5,
      stores: Object.entries(serverState).map(([storeId, state]) =>
        row(Number(storeId), { ...state, pending: true })),
    },
  };
}

beforeEach(() => {
  jest.clearAllMocks();
  jest.useFakeTimers();
  Object.keys(serverState).forEach((key) => delete serverState[key]);
  serverState[1] = { requested_volume_percent: 100, requested_muted: false, last_command_id: 0 };
  serverState[2] = { requested_volume_percent: 100, requested_muted: false, last_command_id: 0 };
  api.get.mockResolvedValue({
    data: { session_id: 5, stores: [row(1, { last_command_id: 0, pending: false }),
                                    row(2, { last_command_id: 0, pending: false })] },
  });
  api.post.mockImplementation(async (url, body) => serverEcho(body));
});

afterEach(() => { jest.useRealTimers(); cleanup(); });

// ===========================================================================
// Coalescing
// ===========================================================================
test("a drag sends the first value at once and then coalesces the rest", async () => {
  await mount();
  api.post.mockClear();

  // Twelve movements in one gesture, faster than the send interval.
  await act(async () => {
    for (const value of [40, 42, 45, 48, 51, 55, 58, 61, 64, 67, 69, 70]) {
      hook.setVolume(1, value);
    }
  });

  // The first one went immediately, so the control feels responsive; the rest
  // collapsed into a single queued send rather than twelve.
  expect(api.post).toHaveBeenCalledTimes(1);
  expect(api.post.mock.calls[0][1]).toEqual({ store_id: 1, volume_percent: 40 });

  await act(async () => { jest.advanceTimersByTime(SEND_INTERVAL_MS + 5); });

  expect(api.post).toHaveBeenCalledTimes(2);
  // The LATEST value, not the second one: intermediates are discarded, never
  // queued, so the network cannot fall behind the operator's hand.
  expect(api.post.mock.calls[1][1]).toEqual({ store_id: 1, volume_percent: 70 });
});

test("a slow Store does not delay another Store", async () => {
  await mount();
  api.post.mockClear();
  // Store 1 is rate-limited; Store 2 must still go out immediately.
  await act(async () => {
    hook.setVolume(1, 30);
    hook.setVolume(1, 35);
    hook.setVolume(2, 80);
  });
  const bodies = api.post.mock.calls.map(([, body]) => body);
  expect(bodies).toContainEqual({ store_id: 1, volume_percent: 30 });
  expect(bodies).toContainEqual({ store_id: 2, volume_percent: 80 });
});

// ===========================================================================
// Independence and mute
// ===========================================================================
test("changing one Store leaves the others untouched", async () => {
  await mount();
  await act(async () => { hook.setVolume(1, 30); });
  expect(hook.states[1].requested_volume_percent).toBe(30);
  expect(hook.states[2].requested_volume_percent).toBe(100);
});

test("mute sends only the mute flag so the chosen level survives", async () => {
  await mount();
  await act(async () => { hook.setVolume(1, 65); });
  await act(async () => { jest.advanceTimersByTime(SEND_INTERVAL_MS + 5); });
  api.post.mockClear();

  await act(async () => { hook.setMuted(1, true); });
  // Deliberately says nothing about volume: the server keeps 65, so unmute
  // has something real to restore rather than a client-side copy.
  expect(api.post.mock.calls[0][1]).toEqual({ store_id: 1, muted: true });
  expect(hook.states[1].requested_volume_percent).toBe(65);
  expect(hook.states[1].requested_muted).toBe(true);
});

// ===========================================================================
// Optimism, and its limit
// ===========================================================================
test("the slider moves at once but nothing is claimed applied", async () => {
  await mount();
  await act(async () => { hook.setVolume(1, 25); });
  expect(hook.states[1].requested_volume_percent).toBe(25);
  // A successful POST means SENT. Only the Store may fill this in.
  expect(hook.states[1].applied_volume_percent).toBeNull();
  expect(hook.states[1].pending).toBe(true);
});

test("an applied acknowledgement is shown as applied", async () => {
  await mount();
  api.post.mockResolvedValue({
    data: { session_id: 5, stores: [row(1, {
      last_command_id: 2, last_acknowledged_command_id: 2,
      applied_volume_percent: 25, applied_muted: false,
      result: "applied", pending: false,
    })] },
  });
  await act(async () => { hook.setVolume(1, 25); });
  expect(hook.states[1].result).toBe("applied");
  expect(hook.states[1].applied_volume_percent).toBe(25);
});

// ===========================================================================
// Stale responses
// ===========================================================================
test("a response describing an older command cannot overwrite a newer one", async () => {
  await mount();
  // The newest command the client knows about.
  await act(async () => {
    api.post.mockResolvedValue({
      data: { session_id: 5, stores: [row(1, {
        last_command_id: 9, requested_volume_percent: 70,
        applied_volume_percent: 70, result: "applied", pending: false,
      })] },
    });
    hook.setVolume(1, 70);
  });
  expect(hook.states[1].applied_volume_percent).toBe(70);

  // A slow reply about command 4 lands afterwards.
  await act(async () => {
    api.get.mockResolvedValue({
      data: { session_id: 5, stores: [row(1, {
        last_command_id: 4, requested_volume_percent: 45,
        applied_volume_percent: 45, result: "applied", pending: false,
      })] },
    });
    await hook.refresh();
  });

  expect(hook.states[1].requested_volume_percent).toBe(70);
  expect(hook.states[1].applied_volume_percent).toBe(70);
});

// ===========================================================================
// Failures
// ===========================================================================
test("a refusal is surfaced rather than snapping the slider back silently", async () => {
  await mount();
  api.post.mockRejectedValue({
    response: { status: 500, data: { detail: "Could not apply output volume" } },
  });
  await act(async () => { hook.setVolume(1, 20); });
  expect(hook.errors[1]).toBe("Could not apply output volume");
  // The slider stays where the operator put it, with the error beside it.
  expect(hook.states[1].requested_volume_percent).toBe(20);
});

test("a finished broadcast is reported in plain words", async () => {
  await mount();
  api.post.mockRejectedValue({
    response: { status: 409, data: { detail: "That broadcast is no longer active." } },
  });
  await act(async () => { hook.setVolume(1, 20); });
  expect(hook.errors[1]).toBe("Broadcast is no longer active");
});

// ===========================================================================
// Gating
// ===========================================================================
test("without the capability nothing is fetched or sent", async () => {
  await mount({ canControl: false });
  expect(api.get).not.toHaveBeenCalled();
  await act(async () => { hook.setVolume(1, 40); });
  expect(api.post).not.toHaveBeenCalled();
});

test("with no live session nothing is fetched or sent", async () => {
  await mount({ sessionId: null });
  expect(api.get).not.toHaveBeenCalled();
  await act(async () => { hook.setVolume(1, 40); });
  expect(api.post).not.toHaveBeenCalled();
});

// ===========================================================================
// Scale
// ===========================================================================
test("a hundred Stores adjusted at once produce one request each", async () => {
  await mount();
  api.post.mockClear();
  await act(async () => {
    for (let storeId = 1; storeId <= 100; storeId += 1) hook.setVolume(storeId, 55);
  });
  // Per-Store rate limiting, not a global one: a hundred Stores is a hundred
  // distinct commands, and none of them waits behind another.
  expect(api.post).toHaveBeenCalledTimes(100);
});

// ===========================================================================
// Capability arrives AFTER the broadcast starts
// ===========================================================================
test("a Store that reports support late stops being shown as unsupported", async () => {
  // The live defect. A Receiver advertises output_volume/output_mute on
  // `receiver_ready`, which it sends in response to HQ's `prepare` - so at the
  // moment a broadcast starts, nothing has advertised anything and every Store
  // legitimately reads supported:false. Fetching once froze that first answer,
  // and because the control is disabled while unsupported there was no POST to
  // correct it: the Console said "Not supported by this Receiver" for a Store
  // whose Receiver had reported full support a second later.
  api.get.mockResolvedValue({
    data: { session_id: 5, stores: [row(1, { supported: false, online: true,
                                             last_command_id: 0, pending: false })] },
  });
  await mount();
  expect(hook.states[1].supported).toBe(false);

  // The Receiver reports READY a moment later and the backend records it.
  api.get.mockResolvedValue({
    data: { session_id: 5, stores: [row(1, { supported: true, online: true,
                                             last_command_id: 0, pending: false })] },
  });
  await act(async () => { jest.advanceTimersByTime(3100); });

  expect(hook.states[1].supported).toBe(true);
});

test("a genuinely unsupported Receiver keeps reading unsupported", async () => {
  // Polling must not turn into optimism: an old Receiver never advertises the
  // capability, and the Console has to keep saying so.
  api.get.mockResolvedValue({
    data: { session_id: 5, stores: [row(1, { supported: false, online: true,
                                             last_command_id: 0, pending: false })] },
  });
  await mount();
  await act(async () => { jest.advanceTimersByTime(9100); });
  expect(hook.states[1].supported).toBe(false);
});

test("polling stops when there is no live session", async () => {
  await mount({ sessionId: null });
  await act(async () => { jest.advanceTimersByTime(9100); });
  expect(api.get).not.toHaveBeenCalled();
});

test("polling stops when the operator lacks the capability", async () => {
  await mount({ canControl: false });
  await act(async () => { jest.advanceTimersByTime(9100); });
  expect(api.get).not.toHaveBeenCalled();
});

// ===========================================================================
// No feedback loop
// ===========================================================================
test("incoming actual-state telemetry never issues a command", async () => {
  // The loop this guards against: HQ sends 80 -> Windows becomes 80 ->
  // Receiver reports 80 -> the client sees 80 and POSTs 80 again -> for ever.
  // Telemetry updates DISPLAYED state only; only an operator gesture sends.
  await mount();
  api.post.mockClear();

  api.get.mockResolvedValue({
    data: { session_id: 5, stores: [row(1, {
      supported: true, online: true, control_status: "ready",
      requested_volume_percent: 80, applied_volume_percent: 80,
      actual_volume_percent: 25, actual_muted: false,
      actual_state_sequence: 3, last_command_id: 2,
      last_acknowledged_command_id: 2, result: "applied", pending: false })] },
  });
  await act(async () => { jest.advanceTimersByTime(3100); });

  expect(hook.states[1].actual_volume_percent).toBe(25);
  expect(api.post).not.toHaveBeenCalled();
});

test("repeated telemetry polls still issue no commands", async () => {
  await mount();
  api.post.mockClear();
  api.get.mockResolvedValue({
    data: { session_id: 5, stores: [row(1, {
      supported: true, actual_volume_percent: 25, actual_muted: false,
      actual_state_sequence: 4, pending: false })] },
  });
  await act(async () => { jest.advanceTimersByTime(12100); });
  expect(api.post).not.toHaveBeenCalled();
});

test("an operator gesture still sends exactly one command", async () => {
  // The other half: telemetry being silent must not make the control inert.
  await mount();
  api.post.mockClear();
  await act(async () => { hook.setVolume(1, 45); });
  expect(api.post).toHaveBeenCalledTimes(1);
  expect(api.post.mock.calls[0][1]).toEqual({ store_id: 1, volume_percent: 45 });
});

test("older telemetry cannot move the displayed state backwards", async () => {
  await mount();
  api.get.mockResolvedValue({
    data: { session_id: 5, stores: [row(1, {
      supported: true, actual_volume_percent: 25, actual_state_sequence: 9,
      last_command_id: 5, pending: false })] },
  });
  await act(async () => { jest.advanceTimersByTime(3100); });
  expect(hook.states[1].actual_volume_percent).toBe(25);

  // A slow response describing an OLDER command arrives afterwards.
  api.get.mockResolvedValue({
    data: { session_id: 5, stores: [row(1, {
      supported: true, actual_volume_percent: 80, actual_state_sequence: 4,
      last_command_id: 2, pending: false })] },
  });
  await act(async () => { jest.advanceTimersByTime(3100); });
  expect(hook.states[1].actual_volume_percent).toBe(25);
});
