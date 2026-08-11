/**
 * The Active Broadcasts supervision page.
 *
 * WHAT THESE TESTS ASSERT, AND WHY IT IS THE DOM
 *
 * The backend tests already prove that a field is not serialized. These prove
 * the other half: that the page renders only what it was given, and does not
 * invent a control for a capability the server did not advertise. The two
 * together are what "permission-controlled" has to mean - one without the
 * other leaves either a leak or a button that 403s when pressed.
 *
 * `meta` on the list response carries the capabilities. It is deliberately
 * the ONLY source: no test here grants a capability by role, because the page
 * must not read one.
 */
import React from "react";
import { render, screen, act, fireEvent, waitFor } from "@testing-library/react";
import ActiveBroadcasts from "./ActiveBroadcasts";

jest.mock("@/lib/api", () => ({
  api: { get: jest.fn(), post: jest.fn() },
}));

// eslint-disable-next-line import/first
const { api } = require("@/lib/api");

const ALICE = {
  session_id: 1, campaign_name: "Morning Offer", started_at: "2026-08-03T10:00:00+00:00",
  status: "live", target_store_count: 1, is_mine: false,
  owner_user_id: 7, owner_username: "alice", owner_display_name: "Alice",
};
const BOB = {
  session_id: 2, campaign_name: "Weekend Offer", started_at: "2026-08-03T10:05:00+00:00",
  status: "live", target_store_count: 3, is_mine: false,
  owner_user_id: 8, owner_username: "bob", owner_display_name: "Bob",
};

/** A row as the backend would send it to somebody without view_ownership:
 *  the owner fields are ABSENT, not null. */
function redactOwner(row) {
  const { owner_user_id, owner_username, owner_display_name, ...rest } = row;
  return rest;
}

function listBody(items, meta) {
  return {
    items, total: items.length, page: 1, page_size: 20,
    pages: 1, has_more: false,
    meta: {
      may_view_ownership: false, may_view_targets: false, may_stop_any: false,
      may_manage_web_audience: false,
      ...meta,
    },
  };
}

async function renderPage({ items = [ALICE, BOB], meta = {}, stores = null } = {}) {
  api.get.mockImplementation(async (path) => {
    if (path.endsWith("/stores")) {
      return { data: stores ?? { session_id: 2, campaign_name: "Weekend Offer",
                                 stores: [], target_store_count: 0 } };
    }
    return { data: listBody(items, meta) };
  });
  render(<ActiveBroadcasts />);
  await act(async () => {});
}

beforeEach(() => {
  jest.clearAllMocks();
  // CRA sets resetMocks, which strips an implementation passed to jest.fn(impl)
  // before every test - so the default has to be installed here.
  api.get.mockImplementation(async () => ({ data: listBody([], {}) }));
  api.post.mockImplementation(async () => ({ data: { ok: true } }));
});

// ===========================================================================
// Ownership visibility (4, 5)
// ===========================================================================
test("without view_ownership there is no Broadcaster column and no name", async () => {
  await renderPage({ items: [redactOwner(ALICE), redactOwner(BOB)] });

  expect(screen.queryByTestId("col-broadcaster")).toBeNull();
  const rendered = document.body.textContent.toLowerCase();
  expect(rendered).not.toContain("alice");
  expect(rendered).not.toContain("bob");
  // The broadcasts themselves are still listed - the page is usable.
  expect(screen.getByTestId("active-row-1")).toBeTruthy();
});

test("with view_ownership the Broadcaster is shown", async () => {
  await renderPage({ meta: { may_view_ownership: true } });

  expect(screen.getByTestId("col-broadcaster")).toBeTruthy();
  expect(screen.getByTestId("active-owner-cell-1").textContent).toContain("Alice");
});

// ===========================================================================
// Target visibility (6, 7, 8)
// ===========================================================================
test("without view_targets there is no View Stores action", async () => {
  await renderPage();
  expect(screen.queryByTestId("active-view-stores-1")).toBeNull();
});

test("with view_targets View Stores is offered", async () => {
  await renderPage({ meta: { may_view_targets: true } });
  expect(screen.getByTestId("active-view-stores-1")).toBeTruthy();
});

test("View Stores shows exact short and full names", async () => {
  await renderPage({
    meta: { may_view_targets: true },
    stores: {
      session_id: 2, campaign_name: "Weekend Offer", target_store_count: 3,
      stores: [
        { store_id: 11, store_code: "BP", store_name: "Testville North" },
        { store_id: 12, store_code: "RG", store_name: "Testville South" },
        { store_id: 13, store_code: "VP", store_name: "Testville East" },
      ],
    },
  });

  await act(async () => { fireEvent.click(screen.getByTestId("active-view-stores-2")); });

  const modal = screen.getByTestId("active-stores-modal");
  expect(modal.textContent).toContain("BP");
  expect(modal.textContent).toContain("Testville North");
  expect(modal.textContent).toContain("VP");
  expect(modal.textContent).toContain("Testville East");
});

// ===========================================================================
// Stop (9, 10, 11, 12)
// ===========================================================================
test("without stop_any there is no cross-owner Stop action", async () => {
  await renderPage({ meta: { may_view_ownership: true, may_view_targets: true } });
  expect(screen.queryByTestId("active-stop-1")).toBeNull();
});

test("with stop_any a selected Stop is offered", async () => {
  await renderPage({ meta: { may_stop_any: true } });
  expect(screen.getByTestId("active-stop-1")).toBeTruthy();
});

test("stop confirmation without view_targets shows a count and no Store names", async () => {
  await renderPage({ meta: { may_stop_any: true }, items: [redactOwner(BOB)] });

  await act(async () => { fireEvent.click(screen.getByTestId("active-stop-2")); });

  const modal = screen.getByTestId("active-stop-modal");
  expect(screen.getByTestId("stop-modal-store-count").textContent).toContain("3");
  // No Store identity anywhere in the dialog.
  for (const leak of ["BP", "Testville", "RG", "VP"]) {
    expect(modal.textContent).not.toContain(leak);
  }
  // And it says Stop this broadcast, never Emergency.
  expect(modal.textContent.toLowerCase()).toContain("stop this broadcast");
  expect(modal.textContent.toLowerCase()).not.toContain("emergency");
});

test("stop confirmation without view_ownership shows no broadcaster identity", async () => {
  await renderPage({ meta: { may_stop_any: true }, items: [redactOwner(BOB)] });

  await act(async () => { fireEvent.click(screen.getByTestId("active-stop-2")); });

  expect(screen.queryByTestId("stop-modal-owner")).toBeNull();
  expect(screen.getByTestId("active-stop-modal").textContent.toLowerCase())
    .not.toContain("bob");
});

test("an explicit DENY is simply an absent capability in meta", async () => {
  // The page cannot tell "ADMIN with DENY" from "never granted", and must
  // not try: the backend already resolved the override, and re-deriving it
  // here from a role is exactly the pattern this design forbids.
  await renderPage({ meta: { may_view_ownership: true, may_view_targets: true,
                             may_stop_any: false } });
  expect(screen.queryByTestId("active-stop-1")).toBeNull();
  expect(screen.getByTestId("active-view-stores-1")).toBeTruthy();
});

// ===========================================================================
// Stop outcomes (22, 23, 24)
// ===========================================================================
test("a successful Stop refreshes from the server rather than dropping the row", async () => {
  await renderPage({ meta: { may_stop_any: true, may_view_ownership: true } });

  // After the stop, the server reports only Alice still live.
  api.get.mockImplementation(async () => ({
    data: listBody([ALICE], { may_stop_any: true, may_view_ownership: true }),
  }));

  await act(async () => { fireEvent.click(screen.getByTestId("active-stop-2")); });
  await act(async () => { fireEvent.click(screen.getByTestId("active-stop-confirm")); });

  await waitFor(() => expect(screen.queryByTestId("active-row-2")).toBeNull());
  expect(api.post).toHaveBeenCalledWith("/broadcast/active-management/2/stop");
  // The other broadcast is untouched.
  expect(screen.getByTestId("active-row-1")).toBeTruthy();
});

test("a failed Stop does not fake success and keeps the dialog open", async () => {
  await renderPage({ meta: { may_stop_any: true } });
  api.post.mockImplementation(async () => {
    const error = new Error("boom");
    error.response = { status: 500, data: { detail: {
      code: "STOP_FAILED",
      message: "This broadcast could not be stopped and may still be live." } } };
    throw error;
  });

  await act(async () => { fireEvent.click(screen.getByTestId("active-stop-2")); });
  await act(async () => { fireEvent.click(screen.getByTestId("active-stop-confirm")); });

  expect(screen.getByTestId("active-stop-error").textContent)
    .toContain("may still be live");
  expect(screen.getByTestId("active-stop-modal")).toBeTruthy();
  expect(screen.queryByTestId("active-notice")).toBeNull();
});

test("a scope refusal is shown honestly", async () => {
  await renderPage({ meta: { may_stop_any: true } });
  api.post.mockImplementation(async () => {
    const error = new Error("denied");
    error.response = { status: 403, data: {
      detail: "This broadcast reaches 1 Store(s) outside your Store Scope." } };
    throw error;
  });

  await act(async () => { fireEvent.click(screen.getByTestId("active-stop-2")); });
  await act(async () => { fireEvent.click(screen.getByTestId("active-stop-confirm")); });

  expect(screen.getByTestId("active-stop-error").textContent)
    .toContain("outside your Store Scope");
});

// ===========================================================================
// Search, filters, paging (16-20)
// ===========================================================================
test("search is sent to the server", async () => {
  await renderPage();
  await act(async () => {
    fireEvent.change(screen.getByTestId("active-search"), { target: { value: "Weekend" } });
  });

  await waitFor(() => {
    expect(api.get).toHaveBeenCalledWith("/broadcast/active-management",
      expect.objectContaining({ params: expect.objectContaining({ q: "Weekend" }) }));
  });
});

test("the search placeholder names only the dimensions the caller may search", async () => {
  await renderPage();
  expect(screen.getByTestId("active-search").getAttribute("placeholder"))
    .toBe("Search broadcast…");
});

test("the placeholder widens with the permissions", async () => {
  await renderPage({ meta: { may_view_ownership: true, may_view_targets: true } });
  expect(screen.getByTestId("active-search").getAttribute("placeholder"))
    .toContain("Store");
});

test("the owner filter is sent to the server and returns to page 1", async () => {
  await renderPage({ meta: { may_view_ownership: true } });
  await act(async () => { fireEvent.click(screen.getByTestId("active-owner-others")); });

  await waitFor(() => {
    expect(api.get).toHaveBeenCalledWith("/broadcast/active-management",
      expect.objectContaining({
        params: expect.objectContaining({ owner: "others", page: 1 }) }));
  });
});

test("sorting is server-side", async () => {
  await renderPage();
  await act(async () => {
    fireEvent.change(screen.getByTestId("active-sort"), { target: { value: "oldest" } });
  });

  await waitFor(() => {
    expect(api.get).toHaveBeenCalledWith("/broadcast/active-management",
      expect.objectContaining({ params: expect.objectContaining({ sort: "oldest" }) }));
  });
});

test("page size is server-side, not a React slice", async () => {
  await renderPage();
  await act(async () => {
    fireEvent.change(screen.getByTestId("active-page-size"), { target: { value: "50" } });
  });

  await waitFor(() => {
    expect(api.get).toHaveBeenCalledWith("/broadcast/active-management",
      expect.objectContaining({ params: expect.objectContaining({ page_size: 50 }) }));
  });
});

test("50 sessions are paginated by the server, not rendered at once", async () => {
  // The server answers with ONE page of 20 out of a total of 50. The page must
  // render 20 rows and report the true total - never fetch everything and
  // slice, which is what this endpoint's design exists to prevent.
  const many = Array.from({ length: 20 }, (_, index) => ({
    ...ALICE, session_id: index + 1, campaign_name: `Campaign ${index + 1}`,
  }));
  api.get.mockImplementation(async () => ({
    data: { items: many, total: 50, page: 1, page_size: 20, pages: 3,
            has_more: true, meta: { may_view_ownership: true } },
  }));
  render(<ActiveBroadcasts />);
  await act(async () => {});

  expect(document.querySelectorAll("tbody tr").length).toBe(20);
  expect(screen.getByTestId("active-total").textContent).toBe("50");
  expect(screen.getByTestId("active-page-info").textContent).toContain("Page 1 of 3");
  expect(screen.getByTestId("active-next").disabled).toBe(false);
});

test("an empty result says so rather than looking broken", async () => {
  await renderPage({ items: [] });
  expect(screen.getByTestId("active-empty")).toBeTruthy();
});

test("a 403 on the list is reported, not rendered as an empty page", async () => {
  api.get.mockImplementation(async () => {
    const error = new Error("denied");
    error.response = { status: 403 };
    throw error;
  });
  render(<ActiveBroadcasts />);
  await act(async () => {});

  expect(screen.getByTestId("active-error").textContent)
    .toContain("do not have permission");
});


// ===========================================================================
// Web Audience supervision
// ===========================================================================

test("the web room summary is shown only with view_ownership", async () => {
  const withRoom = { ...BOB, web_room: {
    public_code: "SL-K7Q92A", status: "OPEN", auto_approve: false,
    password: "Q7KM-92PX", password_available: true,
    waiting_count: 1, connected_count: 2, listening_count: 1 } };

  // The backend does not send web_room at all without view_ownership, so the
  // page cannot show it - which is the point. Here it IS sent.
  await renderPage({ items: [withRoom], meta: { may_view_ownership: true } });
  expect(screen.getByTestId("active-web-room-2").textContent).toContain("SL-K7Q92A");
});

test("no web room appears when the backend redacted it", async () => {
  // Exactly what a caller without view_ownership receives: the key is absent.
  await renderPage({ items: [redactOwner(BOB)], meta: {} });
  expect(screen.queryByTestId("active-web-room-2")).toBeNull();
  expect(screen.queryByText(/EC-/)).toBeNull();
});

test("Web Audience sits beside View Stores for an authorised supervisor", async () => {
  await renderPage({ meta: { may_view_targets: true, may_manage_web_audience: true } });
  expect(screen.getByTestId("active-view-stores-2")).toBeTruthy();
  expect(screen.getByTestId("active-web-audience-2")).toBeTruthy();
});

test("Web Audience is absent without the manage permission", async () => {
  // Reading who is broadcasting is not permission to touch their audience.
  await renderPage({ meta: { may_view_ownership: true, may_view_targets: true } });
  expect(screen.getByTestId("active-view-stores-2")).toBeTruthy();
  expect(screen.queryByTestId("active-web-audience-2")).toBeNull();
});

test("your own broadcast always offers its Web Audience", async () => {
  const mine = { ...ALICE, is_mine: true };
  await renderPage({ items: [mine], meta: {} });
  expect(screen.getByTestId(`active-web-audience-${mine.session_id}`)).toBeTruthy();
});

test("a Link Only broadcast offers Web Audience without View Stores", async () => {
  // Zero Stores, so a View Stores button would be a broken control - but the
  // audience is the whole point of the Broadcast.
  const linkOnly = { ...BOB, session_id: 5, target_store_count: 0 };
  await renderPage({ items: [linkOnly],
                     meta: { may_manage_web_audience: true } });
  expect(screen.getByTestId("active-store-count-5").textContent).toContain("0");
  expect(screen.queryByTestId("active-view-stores-5")).toBeNull();
  expect(screen.getByTestId("active-web-audience-5")).toBeTruthy();
});

test("clicking Web Audience opens the panel for that session", async () => {
  await renderPage({ meta: { may_manage_web_audience: true } });
  await act(async () => {
    fireEvent.click(screen.getByTestId("active-web-audience-2"));
  });
  expect(screen.getByTestId("supervised-audience-modal")).toBeTruthy();
  expect(screen.getByTestId("supervised-audience-campaign").textContent)
    .toContain("Weekend Offer");
});
