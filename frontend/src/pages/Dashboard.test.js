/**
 * The dashboard.
 *
 * Two properties worth holding:
 *
 *   * "today" and "yesterday" are windows with BOTH ends. Expressed as a day
 *     count, yesterday would silently include this morning - the kind of quiet
 *     wrongness a dashboard must not have.
 *   * every report is a chart AND a table. A chart answers "which is biggest"
 *     at a glance and cannot answer "how many exactly", and somebody about to
 *     ring a shop or question a colleague's hours needs the number.
 */
import React from "react";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import Dashboard from "./Dashboard";
import { api } from "@/lib/api";

jest.mock("@/lib/api", () => ({ api: { get: jest.fn() } }));
jest.mock("recharts", () => {
  const Stub = ({ children }) => <div>{children}</div>;
  return {
    ResponsiveContainer: Stub, BarChart: Stub, Bar: Stub, XAxis: Stub,
    YAxis: Stub, Tooltip: Stub, CartesianGrid: Stub, PieChart: Stub,
    Pie: Stub, Cell: Stub, Legend: Stub, LineChart: Stub, Line: Stub,
  };
});

const SUMMARY = {
  since: "2026-08-01T00:00:00+00:00", until: null, days: 30,
  broadcasts: { total: 4, minutes: 12.5, live_now: 1, longest_minutes: 6.2 },
  by_user: [{ user: "Priya", broadcasts: 3, minutes: 9.1 }],
  by_day: [{ day: "2026-08-14", broadcasts: 4, minutes: 12.5 }],
  by_zone: [{ zone: "NORTH", broadcasts: 4, minutes: 12.5, stores: 2 }],
  by_store: [{ store_id: 4, store_code: "NA", store_name: "Nehru Place",
               zone: "NORTH", city: "DELHI", broadcasts: 4, minutes: 12.5 }],
  announcements: { states: { PLAYING: 2, DUCKED: 1, PAUSED: 0, STOPPED: 41 },
                   stores: 44 },
  stores: { total: 44, online: 2, offline: 42 },
};

beforeEach(() => {
  api.get.mockReset();
  api.get.mockImplementation((path) => {
    if (path === "/dashboard/summary") return Promise.resolve({ data: SUMMARY });
    if (path === "/receivers/filter-options") {
      return Promise.resolve({ data: { regions: ["NORTH", "SOUTH"],
                                       cities: ["DELHI"],
                                       stores: [{ id: 4, store_name: "Nehru Place",
                                                  store_code: "NA" }] } });
    }
    if (path === "/users/search") {
      return Promise.resolve({ data: { items: [{ id: 9, username: "priya",
                                                 display_name: "Priya" }] } });
    }
    return Promise.resolve({ data: {} });
  });
});

function summaryCalls() {
  return api.get.mock.calls.filter(([path]) => path === "/dashboard/summary");
}

test("it opens on a period and shows the recorded figures", async () => {
  render(<Dashboard />);
  await screen.findByTestId("tile-broadcasts");

  expect(screen.getByTestId("tile-broadcasts").textContent).toContain("4");
  expect(screen.getByTestId("tile-minutes").textContent).toContain("12.5");
  // The longest single broadcast, not an average: a broadcast nobody stopped
  // is the failure that figure exists to surface.
  expect(screen.getByTestId("tile-minutes").textContent).toContain("6.2");
});

test("today and yesterday are sent as ranges with both ends", async () => {
  render(<Dashboard />);
  await screen.findByTestId("tile-broadcasts");

  fireEvent.click(screen.getByTestId("dashboard-period"));
  fireEvent.click(screen.getByTestId("dashboard-period-option-yesterday"));

  await waitFor(() => {
    const params = summaryCalls().at(-1)[1].params;
    expect(params.since).toBeTruthy();
    expect(params.until).toBe(params.since);
    expect(params.days).toBeUndefined();
  });
});

test("custom dates are only asked for when custom is chosen", async () => {
  render(<Dashboard />);
  await screen.findByTestId("tile-broadcasts");
  expect(screen.queryByTestId("dashboard-since")).toBeNull();

  fireEvent.click(screen.getByTestId("dashboard-period"));
  fireEvent.click(screen.getByTestId("dashboard-period-option-custom"));

  const since = await screen.findByTestId("dashboard-since");
  fireEvent.change(since, { target: { value: "2026-01-01" } });
  await waitFor(() => expect(summaryCalls().at(-1)[1].params.since)
    .toBe("2026-01-01"));
});

test("zone, store and broadcaster filters reach the server", async () => {
  render(<Dashboard />);
  await screen.findByTestId("tile-broadcasts");

  fireEvent.click(screen.getByTestId("dashboard-zone"));
  fireEvent.click(await screen.findByTestId("dashboard-zone-option-NORTH"));
  await waitFor(() => expect(summaryCalls().at(-1)[1].params.zone).toBe("NORTH"));

  fireEvent.click(screen.getByTestId("dashboard-store"));
  fireEvent.click(await screen.findByTestId("dashboard-store-option-4"));
  await waitFor(() => expect(summaryCalls().at(-1)[1].params.store_id).toBe("4"));

  fireEvent.click(screen.getByTestId("dashboard-user"));
  fireEvent.click(await screen.findByTestId("dashboard-user-option-9"));
  await waitFor(() => expect(summaryCalls().at(-1)[1].params.owner_user_id)
    .toBe("9"));
});

test("a filter can name several zones", async () => {
  render(<Dashboard />);
  await screen.findByTestId("tile-broadcasts");

  fireEvent.click(screen.getByTestId("dashboard-zone"));
  fireEvent.click(await screen.findByTestId("dashboard-zone-option-NORTH"));
  fireEvent.click(screen.getByTestId("dashboard-zone-option-SOUTH"));

  await waitFor(() => expect(summaryCalls().at(-1)[1].params.zone)
    .toBe("NORTH,SOUTH"));
});

test("every report is both a chart and a table", async () => {
  render(<Dashboard />);
  await screen.findByTestId("tile-broadcasts");

  for (const key of ["by_day", "by_user", "by_zone", "by_store"]) {
    fireEvent.click(screen.getByTestId(`report-tab-${key}`));
    expect(screen.getByTestId(`report-chart-${key}`)).toBeTruthy();
    expect(screen.getByTestId(`report-table-${key}`)).toBeTruthy();
  }
});

test("the Store report names the shop and its zone", async () => {
  render(<Dashboard />);
  await screen.findByTestId("tile-broadcasts");
  fireEvent.click(screen.getByTestId("report-tab-by_store"));

  const table = screen.getByTestId("report-table-by_store");
  expect(table.textContent).toContain("Nehru Place");
  expect(table.textContent).toContain("NA");
  expect(table.textContent).toContain("NORTH");
});

test("an empty report says so rather than showing an empty table", async () => {
  api.get.mockImplementation((path) => {
    if (path === "/dashboard/summary") {
      return Promise.resolve({ data: { ...SUMMARY, by_store: [] } });
    }
    return Promise.resolve({ data: {} });
  });
  render(<Dashboard />);
  await screen.findByTestId("tile-broadcasts");
  fireEvent.click(screen.getByTestId("report-tab-by_store"));

  expect(screen.getByTestId("report-empty-by_store").textContent)
    .toContain("Nothing in this period");
});

test("a failure is reported rather than leaving an empty page", async () => {
  api.get.mockImplementation((path) => {
    if (path === "/dashboard/summary") {
      return Promise.reject({ response: { data: { detail: "Not allowed." } } });
    }
    return Promise.resolve({ data: {} });
  });
  render(<Dashboard />);
  expect((await screen.findByTestId("dashboard-error")).textContent)
    .toContain("Not allowed.");
});
