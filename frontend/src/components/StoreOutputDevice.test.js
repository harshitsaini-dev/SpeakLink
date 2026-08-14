/**
 * The speaker card, tested for the one thing it must never do: claim a change
 * the Store has not confirmed.
 *
 * Nobody looking at this card can hear the shop. A wrong speaker produces
 * silence, and silence arrives with no error and no failed command - so the
 * card saying "changed" when it only knows "sent" is not a wording problem.
 * It is the difference between somebody checking and somebody not.
 */
import React from "react";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import StoreOutputDevice from "./StoreOutputDevice";
import { api } from "@/lib/api";

jest.mock("@/lib/api", () => ({
  api: { get: jest.fn(), post: jest.fn() },
}));

const permissions = { current: ["receiver.set_output_device"] };
jest.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ can: (code) => permissions.current.includes(code) }),
}));

const REALTEK = {
  index: 3, name: "Speakers (Realtek(R) Audio)", selector: "index:3",
  verified_selector: "index:3@Speakers (Realtek(R) Audio)",
  is_default: true, looks_wireless: false,
};
const HEADSET = {
  index: 5, name: "Bluetooth Headset", selector: "index:5",
  verified_selector: "index:5@Bluetooth Headset",
  is_default: false, looks_wireless: true,
};

function state(overrides = {}) {
  return {
    store_id: 4, devices: [REALTEK, HEADSET], reported_at: "2026-08-14T10:00:00Z",
    requested_selector: null, applied_selector: null, applied_device_name: null,
    previous_selector: null, last_result: null, last_error: null,
    summary: "not reported yet", online: true, ...overrides,
  };
}

beforeEach(() => {
  permissions.current = ["receiver.set_output_device"];
  api.get.mockReset();
  api.post.mockReset();
});

test("only speakers the Store reported can be chosen", async () => {
  api.get.mockResolvedValue({ data: state() });
  render(<StoreOutputDevice storeId={4} />);

  const select = await screen.findByTestId("store-output-select");
  const offered = Array.from(select.querySelectorAll("option"))
    .map((option) => option.value)
    .filter(Boolean);
  expect(offered).toEqual([REALTEK.verified_selector, HEADSET.verified_selector]);

  // No free-text field anywhere: a selector typed by somebody who cannot hear
  // the result may resolve to nothing, or to a different device that exists.
  expect(document.querySelector('input[type="text"]')).toBeNull();
});

test("a Store that has never reported says so instead of offering nothing quietly", async () => {
  api.get.mockResolvedValue({ data: state({ devices: [] }) });
  render(<StoreOutputDevice storeId={4} />);

  const provenance = await screen.findByTestId("store-output-provenance");
  expect(provenance.textContent).toMatch(/has not reported its speakers yet/i);
  expect(screen.queryByTestId("store-output-select")).toBeNull();
});

test("the list is presented as the Store's snapshot, not as HQ's knowledge", async () => {
  api.get.mockResolvedValue({ data: state() });
  render(<StoreOutputDevice storeId={4} />);

  const provenance = await screen.findByTestId("store-output-provenance");
  expect(provenance.textContent).toMatch(/Reported by the Store/i);
  expect(screen.getByTestId("store-output-refresh")).toBeTruthy();
});

test("sending a change reads as sent, never as changed", async () => {
  api.get
    .mockResolvedValueOnce({ data: state() })
    .mockResolvedValue({ data: state({
      requested_selector: HEADSET.verified_selector,
      summary: "a change has been sent and the Store has not answered yet" }) });
  api.post.mockResolvedValue({ data: { note: "Sent. The Store will confirm…" } });

  render(<StoreOutputDevice storeId={4} />);
  fireEvent.change(await screen.findByTestId("store-output-select"),
                   { target: { value: HEADSET.verified_selector } });
  fireEvent.click(screen.getByTestId("store-output-apply"));

  await waitFor(() => expect(screen.getByTestId("store-output-pending").textContent)
    .toMatch(/Waiting for the Store to confirm/i));
  expect(screen.queryByText(/changed successfully/i)).toBeNull();
});

test("a refusal still names the speaker the shop is really on", async () => {
  api.get.mockResolvedValue({ data: state({
    requested_selector: HEADSET.verified_selector,
    applied_selector: REALTEK.verified_selector,
    applied_device_name: REALTEK.name,
    last_result: "refused", last_error: "that endpoint is no longer present",
    summary: "the last change was refused by the Store" }) });

  render(<StoreOutputDevice storeId={4} />);
  const refusal = await screen.findByTestId("store-output-refused");
  expect(refusal.textContent).toMatch(/no longer present/i);
  expect(refusal.textContent).toMatch(/still playing through Speakers \(Realtek/i);
});

test("a wireless speaker is called out before it is sent", async () => {
  api.get.mockResolvedValue({ data: state() });
  render(<StoreOutputDevice storeId={4} />);

  fireEvent.change(await screen.findByTestId("store-output-select"),
                   { target: { value: HEADSET.verified_selector } });
  expect(screen.getByTestId("store-output-wireless-warning").textContent)
    .toMatch(/drop out when the device that owns them leaves/i);

  fireEvent.change(screen.getByTestId("store-output-select"),
                   { target: { value: REALTEK.verified_selector } });
  expect(screen.queryByTestId("store-output-wireless-warning")).toBeNull();
});

test("an account without the right may look but not change", async () => {
  permissions.current = [];
  api.get.mockResolvedValue({ data: state({
    applied_device_name: REALTEK.name,
    summary: `playing through ${REALTEK.name}` }) });

  render(<StoreOutputDevice storeId={4} />);
  const summary = await screen.findByTestId("store-output-summary");
  expect(summary.textContent).toContain(REALTEK.name);
  expect(screen.queryByTestId("store-output-apply")).toBeNull();
  expect(screen.queryByTestId("store-output-refresh")).toBeNull();
  expect(screen.getByTestId("store-output-select").disabled).toBe(true);
});

test("the card says out loud that HQ cannot hear the result", async () => {
  api.get.mockResolvedValue({ data: state() });
  render(<StoreOutputDevice storeId={4} />);
  expect(await screen.findByText(/HQ cannot hear this Store/i)).toBeTruthy();
});

test("a Store that cannot be read reports it rather than rendering nothing", async () => {
  api.get.mockRejectedValue({ response: { data: { detail: "That Store is gone." } } });
  render(<StoreOutputDevice storeId={4} />);
  const failure = await screen.findByTestId("store-output-error");
  expect(failure.textContent).toContain("That Store is gone.");
});
