/**
 * The Master Volume page, and the words it is not allowed to say.
 *
 * This screen shows three genuinely different things that all look like "the
 * volume": what a Store IS, what it WAS, and what HQ WANTS it to be. Almost
 * every assertion below exists because collapsing any two of them would put a
 * confident, plausible, wrong number in front of an operator.
 *
 * The rendered WORDS are asserted rather than internal state, because the
 * words are the product here - "Currently 35%" and "Last known 35%" are the
 * same number and completely different claims.
 */
import React from "react";
import { render, screen, cleanup, waitFor, act, fireEvent } from "@testing-library/react";
import MasterVolume from "./MasterVolume";
import { api } from "@/lib/api";

// Mocked as a NAMED export because that is what the module really has. A
// default-export mock kept these tests green while the production build failed.
jest.mock("@/lib/api", () => ({
  __esModule: true,
  api: { get: jest.fn(), post: jest.fn(), delete: jest.fn() },
}));

function row(overrides = {}) {
  return {
    store_id: 31,
    store_code: "TN",
    store_name: "Testville North",
    zone: "ME ZONE",
    device_id: 13,
    control_status: "ONLINE",
    online: true,
    stale: false,
    volume_percent: 65,
    muted: false,
    level_class: "normal",
    endpoint_status: "ready",
    updated_at: new Date().toISOString(),
    last_seen_at: new Date().toISOString(),
    desired_volume_percent: 65,
    desired_muted: false,
    desired_updated_at: new Date().toISOString(),
    sync_state: "SYNCED",
    sync_error: null,
    ...overrides,
  };
}

function payload(rows, zones = ["ME ZONE"]) {
  return { data: { stores: rows, zones } };
}

async function show(rows, zones) {
  api.get.mockResolvedValue(payload(rows, zones));
  render(<MasterVolume />);
  await screen.findByTestId("master-volume-page");
  await waitFor(() => expect(api.get).toHaveBeenCalled());
}

beforeEach(() => {
  api.get.mockReset();
  api.post.mockReset();
  api.delete.mockReset();
});
afterEach(cleanup);

// ===========================================================================
// Which Stores appear
// ===========================================================================
test("an installed online Store appears with its live level", async () => {
  await show([row({ volume_percent: 65 })]);
  expect(await screen.findByTestId("master-volume-card-TN")).toBeTruthy();
  expect(screen.getByTestId("master-volume-actual-TN").textContent)
    .toBe("Current: 65%");
  expect(screen.getByTestId("master-volume-status-TN").textContent).toBe("ONLINE");
});

test("an installed OFFLINE Store still appears", async () => {
  // The whole point of the page. A shop whose PC is off is the one worth
  // looking at, and an absence would tell the operator nothing.
  await show([row({
    store_code: "TS", store_name: "Testville South", online: false, stale: true,
    control_status: "OFFLINE", volume_percent: 35,
  })]);
  expect(await screen.findByTestId("master-volume-card-TS")).toBeTruthy();
  expect(screen.getByTestId("master-volume-status-TS").textContent).toBe("OFFLINE");
});

test("the Store's zone is shown", async () => {
  await show([row()]);
  // Asserted within the card: the zone filter renders the same text as an
  // option, so a document-wide text query would be ambiguous.
  const card = await screen.findByTestId("master-volume-card-TN");
  expect(card.textContent).toMatch(/ME ZONE/);
});

// ===========================================================================
// THE CONTROLS ARE NEVER DISABLED BY A CONNECTION STATE
// ===========================================================================
test("an OFFLINE Store still has a usable slider", async () => {
  // The defect this rework exists to fix. A manager deciding a shop should be
  // at 30% is not blocked by that shop's PC being switched off.
  await show([row({ online: false, stale: true, control_status: "OFFLINE",
                    volume_percent: 35, desired_volume_percent: 70,
                    sync_state: "WAITING_FOR_SYNC" })]);
  expect((await screen.findByTestId("master-volume-slider-TN")).disabled)
    .toBe(false);
});

test("an OFFLINE Store still has a usable mute button", async () => {
  await show([row({ online: false, stale: true, control_status: "OFFLINE",
                    sync_state: "WAITING_FOR_SYNC" })]);
  expect((await screen.findByTestId("master-volume-mute-TN")).disabled)
    .toBe(false);
});

test("an OFFLINE Store never says immediate control is unavailable", async () => {
  await show([row({ online: false, stale: true, control_status: "OFFLINE",
                    sync_state: "WAITING_FOR_SYNC" })]);
  const note = await screen.findByTestId("master-volume-offline-note-TN");
  expect(note.textContent).toMatch(/will sync when it reconnects/);
  expect(note.textContent).not.toMatch(/unavailable/i);
});

test("a Store with no controllable output can still be given a setting", async () => {
  await show([row({ control_status: "NEEDS_OUTPUT_SELECTION",
                    endpoint_status: "needs_output_selection" })]);
  expect((await screen.findByTestId("master-volume-slider-TN")).disabled)
    .toBe(false);
});

// ===========================================================================
// DESIRED versus ACTUAL - the wording rule
// ===========================================================================
test("an online Store's reading is called Current", async () => {
  await show([row({ volume_percent: 70, desired_volume_percent: 70 })]);
  expect((await screen.findByTestId("master-volume-actual-TN")).textContent)
    .toBe("Current: 70%");
});

test("an offline Store's reading is Last reported, never Current", async () => {
  await show([row({ online: false, stale: true, control_status: "OFFLINE",
                    volume_percent: 35, desired_volume_percent: 70,
                    sync_state: "WAITING_FOR_SYNC" })]);
  const actual = await screen.findByTestId("master-volume-actual-TN");
  expect(actual.textContent).toBe("Last reported: 35%");
  expect(actual.textContent).not.toMatch(/Current/);
});

test("a Store that never reported says its Windows state is Unknown", async () => {
  await show([row({ online: false, stale: true, control_status: "OFFLINE",
                    volume_percent: null, muted: null,
                    desired_volume_percent: 70,
                    sync_state: "WAITING_FOR_SYNC" })]);
  expect((await screen.findByTestId("master-volume-actual-TN")).textContent)
    .toBe("Current Windows state: Unknown");
  // ...and the desired setting is shown anyway.
  expect(screen.getByTestId("master-volume-desired-TN").textContent)
    .toBe("Desired: 70%");
});

test("the desired value is shown separately from the actual one", async () => {
  await show([row({ volume_percent: 35, desired_volume_percent: 70,
                    sync_state: "OUT_OF_SYNC" })]);
  expect((await screen.findByTestId("master-volume-actual-TN")).textContent)
    .toBe("Current: 35%");
  expect(screen.getByTestId("master-volume-desired-TN").textContent)
    .toBe("Desired: 70%");
});

test("nothing ever says Applied", async () => {
  await show([row({ online: false, stale: true, control_status: "OFFLINE",
                    volume_percent: 35, desired_volume_percent: 70,
                    sync_state: "WAITING_FOR_SYNC" })]);
  const card = await screen.findByTestId("master-volume-card-TN");
  expect(card.textContent).not.toMatch(/Applied/i);
});

test("a Store with no HQ setting says so rather than inventing one", async () => {
  await show([row({ desired_volume_percent: null, desired_muted: null,
                    sync_state: "NO_DESIRED_STATE" })]);
  expect((await screen.findByTestId("master-volume-desired-TN")).textContent)
    .toBe("Desired: not set");
});

// ===========================================================================
// Sync wording
// ===========================================================================
test("matching desired and actual reads as Synced", async () => {
  await show([row({ volume_percent: 70, desired_volume_percent: 70,
                    sync_state: "SYNCED" })]);
  expect((await screen.findByTestId("master-volume-sync-TN")).textContent)
    .toBe("Synced");
});

test("an offline difference reads as waiting for sync", async () => {
  await show([row({ online: false, stale: true, control_status: "OFFLINE",
                    volume_percent: 35, desired_volume_percent: 70,
                    sync_state: "WAITING_FOR_SYNC" })]);
  expect((await screen.findByTestId("master-volume-sync-TN")).textContent)
    .toBe("Waiting for Receiver sync");
});

test("a Store changed by its own staff says so, not Applying", async () => {
  // Nothing is being applied. Saying otherwise would promise the operator
  // that HQ was about to act, and HQ deliberately does not fight Store staff.
  await show([row({ volume_percent: 25, desired_volume_percent: 70,
                    sync_state: "OUT_OF_SYNC" })]);
  expect((await screen.findByTestId("master-volume-sync-TN")).textContent)
    .toBe("Changed at the Store");
});

test("a command genuinely in flight reads as Applying", async () => {
  await show([row({ volume_percent: 35, desired_volume_percent: 70,
                    sync_state: "APPLYING" })]);
  expect((await screen.findByTestId("master-volume-sync-TN")).textContent)
    .toBe("Applying…");
});

test("a failed sync is reported with its reason", async () => {
  await show([row({ online: false, stale: true, control_status: "OFFLINE",
                    desired_volume_percent: 70, sync_state: "SYNC_FAILED",
                    sync_error: "the Receiver Device changed" })]);
  expect((await screen.findByTestId("master-volume-sync-TN")).textContent)
    .toBe("Last sync attempt failed");
  expect(screen.getByTestId("master-volume-sync-error-TN").textContent)
    .toBe("the Receiver Device changed");
});

// ===========================================================================
// Control
// ===========================================================================
test("the slider position is the DESIRED value", async () => {
  await show([row({ volume_percent: 25, desired_volume_percent: 70 })]);
  expect((await screen.findByTestId("master-volume-slider-TN")).value).toBe("70");
});

test("moving the slider on an OFFLINE Store still sends the intention", async () => {
  await show([row({ online: false, stale: true, control_status: "OFFLINE",
                    desired_volume_percent: 40,
                    sync_state: "WAITING_FOR_SYNC" })]);
  api.post.mockResolvedValue(payload([row({ online: false, stale: true,
                                            control_status: "OFFLINE",
                                            desired_volume_percent: 70 })]));

  fireEvent.change(await screen.findByTestId("master-volume-slider-TN"),
                   { target: { value: "70" } });
  await waitFor(() => expect(api.post).toHaveBeenCalledWith(
    "/store-audio/master/31", { volume_percent: 70 }));
});

test("mute toggles the DESIRED mute, not the reported one", async () => {
  // The Store currently reports itself unmuted, but HQ already wants it muted.
  // Pressing the button must UNDO the intention, not repeat it.
  await show([row({ muted: false, desired_muted: true })]);
  api.post.mockResolvedValue(payload([row({ desired_muted: false })]));

  fireEvent.click(await screen.findByTestId("master-volume-mute-TN"));
  await waitFor(() => expect(api.post).toHaveBeenCalledWith(
    "/store-audio/master/31", { muted: false }));
});

test("an offline mute is still accepted", async () => {
  await show([row({ online: false, stale: true, control_status: "OFFLINE",
                    desired_muted: false, sync_state: "WAITING_FOR_SYNC" })]);
  api.post.mockResolvedValue(payload([row({ desired_muted: true })]));

  fireEvent.click(await screen.findByTestId("master-volume-mute-TN"));
  await waitFor(() => expect(api.post).toHaveBeenCalledWith(
    "/store-audio/master/31", { muted: true }));
});

test("clearing the HQ setting is offered only when there is one", async () => {
  await show([row({ desired_volume_percent: null, desired_muted: null,
                    sync_state: "NO_DESIRED_STATE" })]);
  await screen.findByTestId("master-volume-card-TN");
  expect(screen.queryByTestId("master-volume-clear-TN")).toBeNull();
});

test("clearing the HQ setting withdraws the intention", async () => {
  await show([row({ desired_volume_percent: 70 })]);
  api.delete.mockResolvedValue(payload([row({ desired_volume_percent: null,
                                              sync_state: "NO_DESIRED_STATE" })]));

  fireEvent.click(await screen.findByTestId("master-volume-clear-TN"));
  await waitFor(() => expect(api.delete).toHaveBeenCalledWith(
    "/store-audio/master/31/pending"));
});

test("a refusal from the server is shown rather than swallowed", async () => {
  await show([row({ control_status: "CONTROLLED_BY_BROADCAST" })]);
  api.post.mockRejectedValue({
    response: { status: 409,
                data: { detail: "That Store is being controlled by an active broadcast." } },
  });

  fireEvent.click(await screen.findByTestId("master-volume-mute-TN"));
  expect((await screen.findByTestId("master-volume-error")).textContent)
    .toMatch(/controlled by an active broadcast/);
});

// ===========================================================================
// NO FEEDBACK LOOP
// ===========================================================================
test("incoming state refreshes never generate a command", async () => {
  jest.useFakeTimers();
  try {
    api.get.mockResolvedValue(payload([row({ volume_percent: 20 })]));
    render(<MasterVolume />);
    await screen.findByTestId("master-volume-page");

    for (let tick = 0; tick < 5; tick += 1) {
      api.get.mockResolvedValue(payload([row({
        volume_percent: 20 + tick, desired_volume_percent: 70,
        sync_state: "OUT_OF_SYNC" })]));
      await act(async () => { jest.advanceTimersByTime(3100); });
    }
    expect(api.post).not.toHaveBeenCalled();
    expect(api.delete).not.toHaveBeenCalled();
  } finally {
    jest.useRealTimers();
  }
});

test("a Store-local change moves ACTUAL and leaves DESIRED alone", async () => {
  jest.useFakeTimers();
  try {
    api.get.mockResolvedValue(payload([row({ volume_percent: 70,
                                             desired_volume_percent: 70 })]));
    render(<MasterVolume />);
    expect((await screen.findByTestId("master-volume-actual-TN")).textContent)
      .toBe("Current: 70%");

    // Somebody at the till drags the Windows slider to 20.
    api.get.mockResolvedValue(payload([row({ volume_percent: 20,
                                             desired_volume_percent: 70,
                                             level_class: "low",
                                             sync_state: "OUT_OF_SYNC" })]));
    await act(async () => { jest.advanceTimersByTime(3100); });

    expect(screen.getByTestId("master-volume-actual-TN").textContent)
      .toBe("Current: 20%");
    expect(screen.getByTestId("master-volume-desired-TN").textContent)
      .toBe("Desired: 70%");
    expect(api.post).not.toHaveBeenCalled();
  } finally {
    jest.useRealTimers();
  }
});

// ===========================================================================
// Filters
// ===========================================================================
test("search narrows by Store code and name", async () => {
  await show([
    row(),
    row({ store_id: 32, store_code: "TS", store_name: "Testville South" }),
  ]);
  await screen.findByTestId("master-volume-card-TN");

  fireEvent.change(screen.getByTestId("master-volume-search"),
                   { target: { value: "south" } });
  await waitFor(() =>
    expect(screen.queryByTestId("master-volume-card-TN")).toBeNull());
  expect(screen.getByTestId("master-volume-card-TS")).toBeTruthy();
});

test("the offline filter narrows the list without disabling anything", async () => {
  await show([
    row(),
    row({ store_id: 32, store_code: "TS", store_name: "Testville South",
          online: false, stale: true, control_status: "OFFLINE",
          sync_state: "WAITING_FOR_SYNC" }),
  ]);
  await screen.findByTestId("master-volume-card-TN");

  fireEvent.change(screen.getByTestId("master-volume-presence"),
                   { target: { value: "offline" } });
  await waitFor(() =>
    expect(screen.queryByTestId("master-volume-card-TN")).toBeNull());
  expect(screen.getByTestId("master-volume-slider-TS").disabled).toBe(false);
});

test("the sync filter finds Stores waiting to be synced", async () => {
  await show([
    row(),
    row({ store_id: 32, store_code: "TS", store_name: "Testville South",
          online: false, stale: true, control_status: "OFFLINE",
          sync_state: "WAITING_FOR_SYNC" }),
  ]);
  await screen.findByTestId("master-volume-card-TN");

  fireEvent.change(screen.getByTestId("master-volume-sync"),
                   { target: { value: "WAITING_FOR_SYNC" } });
  await waitFor(() =>
    expect(screen.queryByTestId("master-volume-card-TN")).toBeNull());
  expect(screen.getByTestId("master-volume-card-TS")).toBeTruthy();
});

test("the default view shows every installed Store, online or not", async () => {
  await show([
    row(),
    row({ store_id: 32, store_code: "TS", store_name: "Testville South",
          online: false, stale: true, control_status: "OFFLINE" }),
  ]);
  expect(await screen.findByTestId("master-volume-card-TN")).toBeTruthy();
  expect(screen.getByTestId("master-volume-card-TS")).toBeTruthy();
  expect(screen.getByTestId("master-volume-count").textContent).toBe("2 of 2 Stores");
});

test("nothing on the page claims a speaker was heard", async () => {
  await show([
    row(),
    row({ store_id: 32, store_code: "TS", store_name: "Testville South",
          online: false, stale: true, control_status: "OFFLINE",
          desired_volume_percent: 70, sync_state: "WAITING_FOR_SYNC" }),
  ]);
  await screen.findByTestId("master-volume-card-TN");
  const page = screen.getByTestId("master-volume-page").textContent.toLowerCase();
  expect(page).not.toMatch(/verified|audible|confirmed heard/);
});
