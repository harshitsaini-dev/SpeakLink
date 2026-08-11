/**
 * Why a Store's output control is unavailable, said accurately.
 *
 * "Not supported by this Receiver" used to cover two completely different
 * situations: a Receiver build that predates master-volume control, and a
 * current build whose Store has simply not re-selected its audio output since
 * upgrading. The remedies are a new Store Kit and half a minute in Store
 * Setup respectively, so telling an operator the wrong one sends them to
 * rebuild software that was already correct.
 *
 * These tests assert the RENDERED words, because the words are the defect.
 */
import React from "react";
import { render, screen, cleanup } from "@testing-library/react";
import StoreAudioControl from "./StoreAudioControl";

const STORE = { id: 101, store_code: "BP", store_name: "Testville North" };

function baseState(overrides = {}) {
  return {
    store_id: 101,
    requested_volume_percent: 100,
    requested_muted: false,
    applied_volume_percent: null,
    applied_muted: null,
    last_command_id: 0,
    last_acknowledged_command_id: 0,
    result: null,
    pending: false,
    control_status: "unknown",
    ...overrides,
  };
}

function show(props = {}) {
  render(
    <StoreAudioControl
      store={STORE}
      state={baseState(props.state)}
      error={props.error}
      online={props.online ?? true}
      supported={props.supported ?? false}
      disabled={false}
      onVolumeChange={() => {}}
      onMuteToggle={() => {}}
    />,
  );
  return screen.getByTestId("store-audio-status-BP");
}

afterEach(cleanup);

// ===========================================================================
// The four states
// ===========================================================================
test("A - a genuinely old Receiver reads as not supported", () => {
  // An older build omits the capabilities block entirely, so the status stays
  // "unknown". That Store really does need a newer Store Kit.
  expect(show({ supported: false, state: { control_status: "unknown" } }).textContent)
    .toBe("Not supported by this Receiver");
});

test("B - a capable Receiver with no endpoint asks for a re-selection", () => {
  // The misleading case. This software CAN control the master; this Store just
  // has not told it which output to control.
  const label = show({ supported: false,
                       state: { control_status: "needs_output_selection" } });
  expect(label.textContent).toBe("Re-select the Store audio output");
  expect(label.textContent).not.toMatch(/Not supported/);
});

test("C - a configured, supported Receiver shows the applied value", () => {
  const label = show({
    supported: true,
    state: { control_status: "ready", result: "applied",
             applied_volume_percent: 60, applied_muted: false },
  });
  expect(label.textContent).toBe("Applied 60%");
  expect(screen.getByTestId("store-volume-BP").disabled).toBe(false);
});

test("D - a configured endpoint that cannot be reached is an honest error", () => {
  const label = show({ supported: false,
                       state: { control_status: "unavailable" } });
  expect(label.textContent).toBe("Store audio output unavailable");
});

// ===========================================================================
// The states must not be optimistic
// ===========================================================================
test("none of the unsupported states enables the controls", () => {
  for (const status of ["unknown", "needs_output_selection", "unavailable"]) {
    cleanup();
    show({ supported: false, state: { control_status: status } });
    expect(screen.getByTestId("store-volume-BP").disabled).toBe(true);
    expect(screen.getByTestId("store-mute-BP").disabled).toBe(true);
  }
});

test("an offline Receiver still reads as offline, whatever its last status", () => {
  // Offline outranks the reason: a Store that is not connected cannot be
  // fixed by re-selecting an output.
  expect(show({ online: false, supported: false,
                state: { control_status: "needs_output_selection" } }).textContent)
    .toBe("Receiver offline");
});

test("a command answered unsupported also says which kind", () => {
  expect(show({ supported: true,
                state: { result: "unsupported",
                         control_status: "needs_output_selection" } }).textContent)
    .toBe("Re-select the Store audio output");
});

test("a failed apply is still reported as failed", () => {
  expect(show({ supported: true,
                state: { result: "failed", control_status: "ready",
                         error_message: "Could not apply output volume" } }).textContent)
    .toBe("Could not apply output volume");
});

test("a pending command reads as sending, never as applied", () => {
  expect(show({ supported: true,
                state: { pending: true, control_status: "ready" } }).textContent)
    .toBe("Sending…");
});

test("nothing in any state claims the speaker was heard", () => {
  for (const status of ["unknown", "needs_output_selection", "unavailable", "ready"]) {
    cleanup();
    const label = show({ supported: status === "ready",
                         state: { control_status: status, result: "applied",
                                  applied_volume_percent: 60 } });
    expect(label.textContent.toLowerCase()).not.toMatch(/verified|audible|heard/);
  }
});

// ===========================================================================
// Two-way sync: the Console follows what the Store is ACTUALLY doing
// ===========================================================================
test("the slider shows the actual Store volume, not the last request", () => {
  // The reported gap: HQ asked for 80, the person at the till moved it to 25,
  // and the Console went on saying 80.
  render(
    <StoreAudioControl
      store={STORE}
      state={baseState({ control_status: "ready", result: "applied",
                         requested_volume_percent: 80,
                         applied_volume_percent: 80, applied_muted: false,
                         actual_volume_percent: 25, actual_muted: false,
                         pending: false })}
      online supported disabled={false}
      onVolumeChange={() => {}} onMuteToggle={() => {}} />,
  );
  expect(screen.getByTestId("store-volume-BP").value).toBe("25");
  expect(screen.getByTestId("store-volume-value-BP").textContent).toBe("25%");
});

test("a stale 'Applied 80%' is not presented as the current truth", () => {
  render(
    <StoreAudioControl
      store={STORE}
      state={baseState({ control_status: "ready", result: "applied",
                         applied_volume_percent: 80, applied_muted: false,
                         actual_volume_percent: 25, actual_muted: false })}
      online supported disabled={false}
      onVolumeChange={() => {}} onMuteToggle={() => {}} />,
  );
  const label = screen.getByTestId("store-audio-status-BP").textContent;
  expect(label).toBe("Currently 25%");
  expect(label).not.toMatch(/Applied 80/);
});

test("a Store muted at the till reads as muted at HQ", () => {
  render(
    <StoreAudioControl
      store={STORE}
      state={baseState({ control_status: "ready", result: "applied",
                         requested_muted: false, applied_muted: false,
                         applied_volume_percent: 80,
                         actual_volume_percent: 80, actual_muted: true })}
      online supported disabled={false}
      onVolumeChange={() => {}} onMuteToggle={() => {}} />,
  );
  expect(screen.getByTestId("store-mute-BP").getAttribute("aria-pressed")).toBe("true");
  expect(screen.getByTestId("store-audio-status-BP").textContent).toBe("Currently muted");
});

test("unmuting at the till reads as unmuted at HQ", () => {
  render(
    <StoreAudioControl
      store={STORE}
      state={baseState({ control_status: "ready", result: "applied",
                         requested_muted: true, applied_muted: true,
                         applied_volume_percent: 65,
                         actual_volume_percent: 65, actual_muted: false })}
      online supported disabled={false}
      onVolumeChange={() => {}} onMuteToggle={() => {}} />,
  );
  expect(screen.getByTestId("store-mute-BP").getAttribute("aria-pressed")).toBe("false");
});

test("a command in flight keeps the operator's value under their finger", () => {
  // Telemetry arriving mid-drag must not yank the slider back to the old
  // value between the drag and the acknowledgement.
  render(
    <StoreAudioControl
      store={STORE}
      state={baseState({ control_status: "ready", pending: true,
                         requested_volume_percent: 90,
                         actual_volume_percent: 25 })}
      online supported disabled={false}
      onVolumeChange={() => {}} onMuteToggle={() => {}} />,
  );
  expect(screen.getByTestId("store-volume-BP").value).toBe("90");
  expect(screen.getByTestId("store-audio-status-BP").textContent).toBe("Sending…");
});

test("with no reading yet the requested value is shown", () => {
  render(
    <StoreAudioControl
      store={STORE}
      state={baseState({ control_status: "ready", requested_volume_percent: 70,
                         actual_volume_percent: null })}
      online supported disabled={false}
      onVolumeChange={() => {}} onMuteToggle={() => {}} />,
  );
  expect(screen.getByTestId("store-volume-BP").value).toBe("70");
});

test("rendering actual state never calls a change handler", () => {
  // The feedback-loop guard at the component level: displaying what the Store
  // reports must not look like an operator moving the control.
  const onVolumeChange = jest.fn();
  const onMuteToggle = jest.fn();
  render(
    <StoreAudioControl
      store={STORE}
      state={baseState({ control_status: "ready", result: "applied",
                         applied_volume_percent: 80,
                         actual_volume_percent: 25, actual_muted: true })}
      online supported disabled={false}
      onVolumeChange={onVolumeChange} onMuteToggle={onMuteToggle} />,
  );
  expect(onVolumeChange).not.toHaveBeenCalled();
  expect(onMuteToggle).not.toHaveBeenCalled();
});
