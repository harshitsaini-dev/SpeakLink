/**
 * Choosing two Zones at once on the Announcements live status.
 *
 * Reported as "only one can be picked". The control itself is multi-select,
 * so the interesting question is whether the PAGE keeps the panel and the
 * chosen set alive across the reload each choice triggers - a panel that
 * vanishes after one tick is indistinguishable from a single-select filter.
 */
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

jest.mock("@/lib/api", () => ({
  api: { get: jest.fn(), post: jest.fn(), delete: jest.fn() },
}));
jest.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ can: () => true, user: { id: 1 } }),
}));

const { api } = require("@/lib/api");
const Announcements = require("./Announcements").default;

beforeEach(() => {
  api.get.mockImplementation((path) => {
    if (path.includes("/announcements/status")) {
      return Promise.resolve({ data: { items: [], total: 0, pages: 1 } });
    }
    if (path.includes("filter-options")) {
      return Promise.resolve({ data: { regions: ["NORTH", "SOUTH"], cities: [], stores: [] } });
    }
    return Promise.resolve({ data: { items: [], total: 0, pages: 1, zones: ["NORTH", "SOUTH"] } });
  });
  api.post.mockResolvedValue({ data: {} });
});

test("two zones can be chosen, and both reach the request", async () => {
  render(<Announcements />);
  const opener = await screen.findByTestId("announcements-zone");
  fireEvent.click(opener);

  fireEvent.click(await screen.findByTestId("announcements-zone-option-NORTH"));
  // The panel must still be open - closing it after one tick is what makes
  // this look like a single-select control.
  const second = await screen.findByTestId("announcements-zone-option-SOUTH");
  fireEvent.click(second);

  await waitFor(() => {
    const sent = api.get.mock.calls
      .filter(([path]) => path.includes("/announcements/status"))
      .map(([, config]) => config?.params?.zone);
    expect(sent).toContain("NORTH,SOUTH");
  });
});
