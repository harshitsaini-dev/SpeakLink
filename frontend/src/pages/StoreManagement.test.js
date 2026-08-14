/**
 * Store Management search and filter.
 *
 * The property worth testing is not "a box appears" but that the box reaches
 * the SERVER. Store Management previously loaded the whole catalog and
 * filtered nothing; filtering it in React would mean the narrowing an
 * operator sees and the narrowing the backend enforces are two different
 * things, and a scoped account could be shown a total that includes Stores it
 * may not open.
 */
import React from "react";
import { render, screen, act, fireEvent, waitFor } from "@testing-library/react";
import StoreManagement from "./StoreManagement";
import { api } from "@/lib/api";

// react-router-dom does not resolve under this Jest configuration, and the
// page needs exactly one thing from it: a Link. Mocking that is smaller and
// more honest than wiring a router this test does not exercise.
jest.mock("react-router-dom", () => ({
  Link: ({ children, to, ...rest }) => <a href={to} {...rest}>{children}</a>,
}), { virtual: true });
jest.mock("@/lib/api", () => ({ api: { get: jest.fn(), post: jest.fn(), put: jest.fn() } }));
jest.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ can: () => true, user: { id: 1, role: "OWNER" } }),
}));

const STORES = [
  { id: 1, store_code: "T1", store_name: "Test Store One", city: "TESTCITY", region: "TESTZONE",
    is_online_store: false, is_active: true, lifecycle_state: "active" },
  { id: 2, store_code: "T2", store_name: "Test Store Two", city: "OTHERCITY", region: "OTHERZONE",
    is_online_store: false, is_active: true, lifecycle_state: "active" },
];

function respond({ items = STORES, total = items.length, pages = 1, has_more = false } = {}) {
  api.get.mockImplementation((path) => {
    if (String(path).startsWith("/stores/filter-options")) {
      return Promise.resolve({ data: { regions: ["TESTZONE", "OTHERZONE"], cities: ["TESTCITY", "OTHERCITY"] } });
    }
    return Promise.resolve({ data: { items, total, page: 1, page_size: 50, pages, has_more } });
  });
}

const searchCalls = () =>
  api.get.mock.calls.filter(([path]) => String(path).startsWith("/stores/search"));

async function renderPage() {
  render(<StoreManagement />);
  await act(async () => {});
}

afterEach(() => jest.clearAllMocks());

test("the search box, Zone and City filters are rendered", async () => {
  respond();
  await renderPage();
  expect(screen.getByTestId("stores-search")).toBeTruthy();
  expect(screen.getByTestId("stores-zone")).toBeTruthy();
  expect(screen.getByTestId("stores-city")).toBeTruthy();
});

test("the result count comes from the server total, not the visible rows", async () => {
  // 2 rows on this page, 44 in the estate. Counting the rows would report 2.
  respond({ items: STORES, total: 44, pages: 22, has_more: true });
  await renderPage();
  expect(screen.getByTestId("result-count").textContent).toContain("44");
});

test("typing a search sends q to the server rather than filtering in React", async () => {
  respond();
  await renderPage();
  await act(async () => {
    fireEvent.change(screen.getByTestId("stores-search"), { target: { value: "test store" } });
  });
  await waitFor(() => {
    const last = searchCalls().at(-1);
    expect(last[1].params).toMatchObject({ q: "test store" });
  });
});

test("choosing a Zone sends region to the server", async () => {
  respond();
  await renderPage();
  await act(async () => { fireEvent.click(screen.getByTestId("stores-zone")); });
  await act(async () => {
    fireEvent.click(screen.getByTestId("stores-zone-option-TESTZONE"));
  });
  await waitFor(() => {
    expect(searchCalls().at(-1)[1].params).toMatchObject({ region: "TESTZONE" });
  });
});

test("choosing a City sends city to the server", async () => {
  respond();
  await renderPage();
  await act(async () => { fireEvent.click(screen.getByTestId("stores-city")); });
  await act(async () => {
    fireEvent.click(screen.getByTestId("stores-city-option-TESTCITY"));
  });
  await waitFor(() => {
    expect(searchCalls().at(-1)[1].params).toMatchObject({ city: "TESTCITY" });
  });
});

test("Store Management defaults to the Active lifecycle", async () => {
  respond();
  await renderPage();
  expect(searchCalls()[0][1].params).toMatchObject({ lifecycle: "active" });
  // The old pair of overlapping switches is gone.
  expect(searchCalls()[0][1].params).not.toHaveProperty("include_inactive");
  expect(searchCalls()[0][1].params).not.toHaveProperty("include_archived");
});

test("there is exactly one lifecycle control, with no duplicate or deleted option", async () => {
  respond();
  await renderPage();
  await act(async () => { fireEvent.click(screen.getByTestId("stores-lifecycle")); });
  const panel = screen.getByTestId("stores-lifecycle-panel");
  const labels = [...panel.querySelectorAll("button, label")]
    .map((node) => node.textContent.trim())
    .filter((text) => text.length);
  expect(labels).toEqual(["All Current", "Active", "Disabled", "Archived"]);
  // No "Permanent Deleted", and no empty placeholder that would mean the
  // same as one of the real states.
  expect(labels.some((l) => /delete/i.test(l))).toBe(false);
  expect(new Set(labels).size).toBe(labels.length);
});

test("a lifecycle stays in effect only while it is ticked", async () => {
  // The original bug was a previous selection staying in effect INVISIBLY.
  // The filter takes several states now - "active and archived" is an
  // ordinary question - so what protects against that bug is no longer
  // exclusivity but the fact that every chosen value is on screen. Unticking
  // one has to actually remove it.
  respond();
  await renderPage();

  // The page opens on Active, so that one is already ticked.
  await act(async () => { fireEvent.click(screen.getByTestId("stores-lifecycle")); });
  await act(async () => {
    fireEvent.click(screen.getByTestId("stores-lifecycle-option-archived"));
  });
  await waitFor(() => expect(searchCalls().at(-1)[1].params.lifecycle)
    .toBe("active,archived"));

  // Unticking one has to actually remove it - that is what the original bug
  // was about, and it is the property that survives the change to checkboxes.
  await act(async () => {
    fireEvent.click(screen.getByTestId("stores-lifecycle-option-active"));
  });
  await waitFor(() => {
    const params = searchCalls().at(-1)[1].params;
    expect(params.lifecycle).toBe("archived");
    expect(JSON.stringify(params)).not.toContain("active");
  });
});

test("changing a filter returns to page 1", async () => {
  respond({ items: STORES, total: 200, pages: 4, has_more: true });
  await renderPage();

  await act(async () => { fireEvent.click(screen.getByTestId("page-next")); });
  await waitFor(() => expect(searchCalls().at(-1)[1].params.page).toBe(2));

  await act(async () => { fireEvent.click(screen.getByTestId("stores-zone")); });
  await act(async () => {
    fireEvent.click(screen.getByTestId("stores-zone-option-OTHERZONE"));
  });
  await waitFor(() => {
    const last = searchCalls().at(-1)[1].params;
    expect(last.page).toBe(1);
    expect(last.region).toBe("OTHERZONE");
  });
});

test("Clear Filters returns to the default Active view", async () => {
  respond();
  await renderPage();
  await act(async () => {
    fireEvent.change(screen.getByTestId("stores-search"), { target: { value: "x" } });
  });
  await waitFor(() => expect(screen.getByTestId("clear-filters")).toBeTruthy());
  await act(async () => { fireEvent.click(screen.getByTestId("clear-filters")); });
  await waitFor(() => {
    const params = searchCalls().at(-1)[1].params;
    expect(params).not.toHaveProperty("q");
    expect(params.lifecycle).toBe("active");
  });
});

test("an empty result says so and does not look like a failure", async () => {
  respond({ items: [], total: 0 });
  await renderPage();
  expect(screen.getByTestId("list-empty")).toBeTruthy();
  expect(screen.queryByTestId("list-error")).toBeNull();
});

test("a refused request reports a permission problem rather than 'no Stores'", async () => {
  api.get.mockImplementation((path) => {
    if (String(path).startsWith("/stores/filter-options")) {
      return Promise.resolve({ data: { regions: [], cities: [] } });
    }
    return Promise.reject({ response: { status: 403 } });
  });
  await renderPage();
  expect(screen.getByTestId("list-error").textContent).toMatch(/permission/i);
  expect(screen.queryByTestId("list-empty")).toBeNull();
});

test("the Zone and City options come from the server, not from the visible page", async () => {
  // Only one Zone is on screen, but the account may reach the other too.
  respond({ items: [STORES[0]], total: 1 });
  await renderPage();
  // The options live in the panel now, not in the button's own text - the
  // button says what is CHOSEN, which is nothing yet.
  await act(async () => { fireEvent.click(screen.getByTestId("stores-zone")); });
  expect(screen.getByTestId("stores-zone-option-OTHERZONE")).toBeTruthy();
  await act(async () => { fireEvent.click(screen.getByTestId("stores-city")); });
  expect(screen.getByTestId("stores-city-option-OTHERCITY")).toBeTruthy();
});

test("no Receiver online/offline status is claimed on this screen", async () => {
  // Store lifecycle is a Store fact; Receiver connection is live WebSocket
  // state and belongs to Receiver Status.
  respond();
  await renderPage();
  // Both seeded Stores are ACTIVE, so there are two badges - the point is
  // that lifecycle is shown and Receiver connection is not.
  expect(screen.getAllByTestId("lifecycle-ACTIVE").length).toBe(2);
  expect(screen.queryByText(/^online$/i)).toBeNull();
});
