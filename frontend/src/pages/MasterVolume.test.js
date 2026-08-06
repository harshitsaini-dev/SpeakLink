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
    store_code: "BP",
    store_name: "Bindapur",
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
    pending_volume_percent: null,
    pending_muted: null,
    pending_created_at: null,
    pending_status: null,
    pending_error: null,
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
  expect(await screen.findByTestId("master-volume-card-BP")).toBeTruthy();
  expect(screen.getByTestId("master-volume-value-BP").textContent).toBe("65%");
  expect(screen.getByTestId("master-volume-status-BP").textContent).toBe("ONLINE");
});

test("an installed OFFLINE Store still appears", async () => {
  // The whole point of the page. A shop whose PC is off is the one worth
  // looking at, and an absence would tell the operator nothing.
  await show([row({
    store_code: "RG", store_name: "Rajouri Garden", online: false, stale: true,
    control_status: "OFFLINE", volume_percent: 35,
  })]);
  expect(await screen.findByTestId("master-volume-card-RG")).toBeTruthy();
  expect(screen.getByTestId("master-volume-status-RG").textContent).toBe("OFFLINE");
});

test("the Store's zone is shown", async () => {
  await show([row()]);
  // Asserted within the card: the zone filter renders the same text as an
  // option, so a document-wide text query would be ambiguous.
  const card = await screen.findByTestId("master-volume-card-BP");
  expect(card.textContent).toMatch(/ME ZONE/);
});

// ===========================================================================
// Current versus remembered - the wording rule
// ===========================================================================
test("an online Store says Currently", async () => {
  await show([row({ volume_percent: 65 })]);
  expect((await screen.findByTestId("master-volume-freshness-BP")).textContent)
    .toBe("Currently 65%");
});

test("an offline Store says Last known and never Currently", async () => {
  await show([row({ online: false, stale: true, control_status: "OFFLINE",
                    volume_percent: 35 })]);
  const freshness = await screen.findByTestId("master-volume-freshness-BP");
  expect(freshness.textContent).toBe("Last known");
  expect(freshness.textContent).not.toMatch(/Currently/);
  // The number is still shown - knowing a shop was left at 35% is useful.
  expect(screen.getByTestId("master-volume-value-BP").textContent).toBe("35%");
});

test("an offline Store says immediate control is unavailable", async () => {
  await show([row({ online: false, stale: true, control_status: "OFFLINE" })]);
  expect((await screen.findByTestId("master-volume-offline-note-BP")).textContent)
    .toBe("Immediate control unavailable.");
});

test("a Store that has never reported shows no invented number", async () => {
  await show([row({ online: false, stale: true, control_status: "OFFLINE",
                    volume_percent: null, muted: null })]);
  expect((await screen.findByTestId("master-volume-value-BP")).textContent).toBe("—");
  expect(screen.getByTestId("master-volume-freshness-BP").textContent)
    .toBe("Never reported");
});

test("a muted Store is labelled muted", async () => {
  await show([row({ muted: true })]);
  expect(await screen.findByText("Muted")).toBeTruthy();
});

// ===========================================================================
// Control
// ===========================================================================
test("moving the slider sends exactly one command", async () => {
  await show([row({ volume_percent: 65 })]);
  api.post.mockResolvedValue(payload([row({ volume_percent: 65 })]));

  const slider = await screen.findByTestId("master-volume-slider-BP");
  fireEvent.change(slider, { target: { value: "70" } });

  await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
  expect(api.post).toHaveBeenCalledWith("/store-audio/master/31",
                                        { volume_percent: 70 });
});

test("mute sends the opposite of what the Store currently reports", async () => {
  await show([row({ muted: false })]);
  api.post.mockResolvedValue(payload([row({ muted: true })]));

  fireEvent.click(await screen.findByTestId("master-volume-mute-BP"));
  await waitFor(() => expect(api.post).toHaveBeenCalledWith(
    "/store-audio/master/31", { muted: true }));
});

test("an offline Store's controls are disabled", async () => {
  await show([row({ online: false, stale: true, control_status: "OFFLINE" })]);
  expect((await screen.findByTestId("master-volume-slider-BP")).disabled).toBe(true);
  expect(screen.getByTestId("master-volume-mute-BP").disabled).toBe(true);
});

test("a Store with no selected output cannot be controlled", async () => {
  await show([row({ control_status: "NEEDS_OUTPUT_SELECTION",
                    endpoint_status: "needs_output_selection" })]);
  expect((await screen.findByTestId("master-volume-slider-BP")).disabled).toBe(true);
  expect(screen.getByTestId("master-volume-status-BP").textContent)
    .toBe("Re-select the Store audio output");
});

test("an unavailable output is reported honestly", async () => {
  await show([row({ control_status: "OUTPUT_UNAVAILABLE",
                    endpoint_status: "unavailable" })]);
  expect((await screen.findByTestId("master-volume-status-BP")).textContent)
    .toBe("Store audio output unavailable");
});

// ===========================================================================
// NO FEEDBACK LOOP
// ===========================================================================
test("incoming state refreshes never generate a command", async () => {
  // The loop this guards against: HQ draws a reading, answers it with a
  // command, hears the resulting reading, and never stops.
  jest.useFakeTimers();
  try {
    api.get.mockResolvedValue(payload([row({ volume_percent: 20 })]));
    render(<MasterVolume />);
    await screen.findByTestId("master-volume-page");

    for (let tick = 0; tick < 5; tick += 1) {
      api.get.mockResolvedValue(payload([row({ volume_percent: 20 + tick })]));
      await act(async () => { jest.advanceTimersByTime(3100); });
    }
    expect(api.post).not.toHaveBeenCalled();
    expect(api.delete).not.toHaveBeenCalled();
  } finally {
    jest.useRealTimers();
  }
});

test("a Store-local change moves the page with no interaction at all", async () => {
  jest.useFakeTimers();
  try {
    api.get.mockResolvedValue(payload([row({ volume_percent: 70 })]));
    render(<MasterVolume />);
    expect((await screen.findByTestId("master-volume-value-BP")).textContent)
      .toBe("70%");

    // Somebody at the till drags the Windows slider to 20.
    api.get.mockResolvedValue(payload([row({ volume_percent: 20, level_class: "low" })]));
    await act(async () => { jest.advanceTimersByTime(3100); });

    expect(screen.getByTestId("master-volume-value-BP").textContent).toBe("20%");
    expect(api.post).not.toHaveBeenCalled();
  } finally {
    jest.useRealTimers();
  }
});

// ===========================================================================
// Pending on reconnect
// ===========================================================================
test("a pending change says pending, never applied", async () => {
  await show([row({
    online: false, stale: true, control_status: "OFFLINE", volume_percent: 35,
    pending_volume_percent: 70, pending_muted: false, pending_status: "pending",
  })]);
  const pending = await screen.findByTestId("master-volume-pending-BP");
  expect(pending.textContent).toMatch(/Pending — will apply when Receiver reconnects/);
  expect(pending.textContent).toMatch(/70%/);
  expect(pending.textContent).not.toMatch(/Applied/);
  expect(pending.textContent).not.toMatch(/Currently 70/);
});

test("a failed pending attempt stays truthful", async () => {
  await show([row({
    online: false, stale: true, control_status: "OFFLINE",
    pending_volume_percent: 70, pending_status: "failed",
    pending_error: "the Receiver Device changed",
  })]);
  const pending = await screen.findByTestId("master-volume-pending-BP");
  expect(pending.textContent).toMatch(/last attempt failed/);
  expect(pending.textContent).toMatch(/the Receiver Device changed/);
  expect(pending.textContent).not.toMatch(/Applied/);
});

test("Cancel Pending Change withdraws it", async () => {
  await show([row({
    online: false, stale: true, control_status: "OFFLINE",
    pending_volume_percent: 70, pending_status: "pending",
  })]);
  api.delete.mockResolvedValue(payload([row({
    online: false, stale: true, control_status: "OFFLINE" })]));

  fireEvent.click(await screen.findByTestId("master-volume-cancel-BP"));
  await waitFor(() => expect(api.delete).toHaveBeenCalledWith(
    "/store-audio/master/31/pending"));
  await waitFor(() =>
    expect(screen.queryByTestId("master-volume-pending-BP")).toBeNull());
});

test("a Store with no pending change shows no pending block", async () => {
  await show([row()]);
  await screen.findByTestId("master-volume-card-BP");
  expect(screen.queryByTestId("master-volume-pending-BP")).toBeNull();
});

// ===========================================================================
// Active broadcast ownership
// ===========================================================================
test("a Store an active broadcast owns says so and stays visible", async () => {
  await show([row({ control_status: "CONTROLLED_BY_BROADCAST" })]);
  expect((await screen.findByTestId("master-volume-status-BP")).textContent)
    .toBe("Controlled by active broadcast");
  // Still visible, and still controllable - the request is routed through the
  // broadcast's own authority rather than a second competing channel.
  expect(screen.getByTestId("master-volume-slider-BP").disabled).toBe(false);
});

test("a refusal from the server is shown rather than swallowed", async () => {
  await show([row({ control_status: "CONTROLLED_BY_BROADCAST" })]);
  api.post.mockRejectedValue({
    response: { status: 409,
                data: { detail: "That Store is being controlled by an active broadcast." } },
  });

  fireEvent.click(await screen.findByTestId("master-volume-mute-BP"));
  expect((await screen.findByTestId("master-volume-error")).textContent)
    .toMatch(/controlled by an active broadcast/);
});

// ===========================================================================
// Filters
// ===========================================================================
test("search narrows by Store code and name", async () => {
  await show([
    row(),
    row({ store_id: 32, store_code: "RG", store_name: "Rajouri Garden" }),
  ]);
  await screen.findByTestId("master-volume-card-BP");

  fireEvent.change(screen.getByTestId("master-volume-search"),
                   { target: { value: "rajouri" } });
  await waitFor(() =>
    expect(screen.queryByTestId("master-volume-card-BP")).toBeNull());
  expect(screen.getByTestId("master-volume-card-RG")).toBeTruthy();
});

test("the offline filter shows only offline Stores", async () => {
  await show([
    row(),
    row({ store_id: 32, store_code: "RG", store_name: "Rajouri Garden",
          online: false, stale: true, control_status: "OFFLINE" }),
  ]);
  await screen.findByTestId("master-volume-card-BP");

  fireEvent.change(screen.getByTestId("master-volume-presence"),
                   { target: { value: "offline" } });
  await waitFor(() =>
    expect(screen.queryByTestId("master-volume-card-BP")).toBeNull());
  expect(screen.getByTestId("master-volume-card-RG")).toBeTruthy();
});

test("the endpoint filter finds Stores needing a re-selection", async () => {
  await show([
    row(),
    row({ store_id: 32, store_code: "RG", store_name: "Rajouri Garden",
          control_status: "NEEDS_OUTPUT_SELECTION",
          endpoint_status: "needs_output_selection" }),
  ]);
  await screen.findByTestId("master-volume-card-BP");

  fireEvent.change(screen.getByTestId("master-volume-endpoint"),
                   { target: { value: "needs_output_selection" } });
  await waitFor(() =>
    expect(screen.queryByTestId("master-volume-card-BP")).toBeNull());
  expect(screen.getByTestId("master-volume-card-RG")).toBeTruthy();
});

test("the default view shows every installed Store, online or not", async () => {
  await show([
    row(),
    row({ store_id: 32, store_code: "RG", store_name: "Rajouri Garden",
          online: false, stale: true, control_status: "OFFLINE" }),
  ]);
  expect(await screen.findByTestId("master-volume-card-BP")).toBeTruthy();
  expect(screen.getByTestId("master-volume-card-RG")).toBeTruthy();
  expect(screen.getByTestId("master-volume-count").textContent).toBe("2 of 2 Stores");
});

// ===========================================================================
// The number stays authoritative
// ===========================================================================
test("a low Store still shows its exact percentage", async () => {
  await show([row({ volume_percent: 12, level_class: "low" })]);
  // The class is a scanning aid for forty Stores. It never replaces the number.
  expect((await screen.findByTestId("master-volume-value-BP")).textContent)
    .toBe("12%");
});

test("nothing on the page claims a speaker was heard", async () => {
  await show([
    row(),
    row({ store_id: 32, store_code: "RG", store_name: "Rajouri Garden",
          online: false, stale: true, control_status: "OFFLINE",
          pending_volume_percent: 70, pending_status: "pending" }),
  ]);
  await screen.findByTestId("master-volume-card-BP");
  const page = screen.getByTestId("master-volume-page").textContent.toLowerCase();
  expect(page).not.toMatch(/verified|audible|confirmed heard/);
});
