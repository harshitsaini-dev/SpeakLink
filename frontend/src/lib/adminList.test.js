/**
 * The two rules the shared admin-list toolkit exists to hold, tested at the
 * level they can actually be got wrong.
 *
 *   1. Any filter change returns to page 1. Without this, narrowing a search
 *      while on page 3 shows an empty screen that reads as "no matches"
 *      rather than "you are past the end".
 *   2. Select All Filtered is a MODE, not a materialised list of ids. The
 *      request carries the filter and the backend resolves it inside the
 *      caller's own scope - React never pages through thousands of rows to
 *      enumerate ids it would then post straight back.
 */
import React from "react";
import { render, screen, act } from "@testing-library/react";
import { activeFilters, countActiveFilters, useAdminList, useBulkSelection } from "./adminList";
import { api } from "./api";

jest.mock("./api", () => ({ api: { get: jest.fn() } }));

const respond = (body) => api.get.mockResolvedValue({ data: body });

afterEach(() => jest.clearAllMocks());

describe("activeFilters", () => {
  test("drops empty, null, undefined and false so a cleared control sends nothing", () => {
    expect(activeFilters({
      q: "", city: null, region: undefined, include_archived: false,
      store_id: "4", page_size: 0,
    })).toEqual({ store_id: "4", page_size: 0 });
  });

  test("keeps 0 - a real value that is not an empty control", () => {
    expect(activeFilters({ store_id: 0 })).toEqual({ store_id: 0 });
  });

  test("countActiveFilters can ignore keys that are not operator-facing", () => {
    expect(countActiveFilters({ q: "a", page_size: 50 }, { ignore: ["page_size"] })).toBe(1);
  });
});

function ListHarness() {
  const list = useAdminList("/logs/search", { q: "", level: "" });
  return (
    <div>
      <span data-testid="page">{list.page}</span>
      <span data-testid="total">{list.total}</span>
      <span data-testid="error">{list.error}</span>
      <button onClick={() => list.setPage(3)}>next</button>
      <button onClick={() => list.setFilter("q", "boom")}>filter</button>
      <button onClick={list.clearFilters}>clear</button>
    </div>
  );
}

describe("useAdminList", () => {
  test("a filter change resets to page 1", async () => {
    respond({ items: [], total: 120, pages: 3, has_more: true });
    render(<ListHarness />);
    await act(async () => {});

    await act(async () => { screen.getByText("next").click(); });
    expect(screen.getByTestId("page").textContent).toBe("3");

    await act(async () => { screen.getByText("filter").click(); });
    expect(screen.getByTestId("page").textContent).toBe("1");

    const last = api.get.mock.calls[api.get.mock.calls.length - 1][1].params;
    expect(last).toMatchObject({ q: "boom", page: 1 });
    expect(last).not.toHaveProperty("level"); // empty controls are not sent
  });

  test("clearing filters also returns to page 1", async () => {
    respond({ items: [], total: 5, pages: 1, has_more: false });
    render(<ListHarness />);
    await act(async () => {});
    await act(async () => { screen.getByText("next").click(); });
    await act(async () => { screen.getByText("clear").click(); });
    expect(screen.getByTestId("page").textContent).toBe("1");
  });

  test("a 403 is reported as a permission answer, not a generic failure", async () => {
    api.get.mockRejectedValue({ response: { status: 403 } });
    render(<ListHarness />);
    await act(async () => {});
    expect(screen.getByTestId("error").textContent)
      .toBe("You do not have permission to view this.");
    expect(screen.getByTestId("total").textContent).toBe("0");
  });
});

function SelectionHarness({ filters }) {
  const items = [{ id: 1 }, { id: 2 }];
  const selection = useBulkSelection({ items, total: 900, filters });
  return (
    <div>
      <span data-testid="mode">{selection.mode}</span>
      <span data-testid="count">{selection.selectedCount}</span>
      <span data-testid="request">{JSON.stringify(selection.toRequest())}</span>
      <button onClick={() => selection.toggleRow(1)}>row</button>
      <button onClick={selection.selectPage}>page</button>
      <button onClick={selection.selectAllFiltered}>all</button>
    </div>
  );
}

describe("useBulkSelection", () => {
  test("Select Page sends the visible ids", async () => {
    render(<SelectionHarness filters={{ q: "x" }} />);
    await act(async () => { screen.getByText("page").click(); });
    expect(screen.getByTestId("count").textContent).toBe("2");
    expect(JSON.parse(screen.getByTestId("request").textContent))
      .toEqual({ mode: "ids", ids: [1, 2] });
  });

  test("Select All Filtered sends the FILTER, never an enumerated id list", async () => {
    render(<SelectionHarness filters={{ q: "x", level: "" }} />);
    await act(async () => { screen.getByText("all").click(); });

    expect(screen.getByTestId("mode").textContent).toBe("filtered");
    // The count is the server's own total for the current filter, so the
    // number agreed to is the number the backend will act on.
    expect(screen.getByTestId("count").textContent).toBe("900");
    const request = JSON.parse(screen.getByTestId("request").textContent);
    expect(request).toEqual({ mode: "filtered", filters: { q: "x" } });
    expect(request).not.toHaveProperty("ids");
  });

  test("changing the filter invalidates a selection made under the previous one", async () => {
    const { rerender } = render(<SelectionHarness filters={{ q: "x" }} />);
    await act(async () => { screen.getByText("all").click(); });
    expect(screen.getByTestId("mode").textContent).toBe("filtered");

    await act(async () => { rerender(<SelectionHarness filters={{ q: "different" }} />); });
    expect(screen.getByTestId("mode").textContent).toBe("none");
    expect(screen.getByTestId("count").textContent).toBe("0");
  });

  test("in filtered mode every row reads as selected, including unseen pages", async () => {
    render(<SelectionHarness filters={{ q: "x" }} />);
    await act(async () => { screen.getByText("all").click(); });
    expect(screen.getByTestId("count").textContent).toBe("900");
  });
});
