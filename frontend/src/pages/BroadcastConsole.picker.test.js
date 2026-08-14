/**
 * The Broadcast Console Store picker: filtering, pagination and selection.
 *
 * The real fleet is around forty Stores. A flat table of forty rows with one
 * search box is not a picker, and the two things that break first when one is
 * added are selection surviving a page change and a Zone FILTER quietly turning
 * into Zone TARGETING. Both are tested here.
 *
 * Also covers the defect this milestone exists for: Online Stores Only read the
 * Online / Physical business flag instead of Receiver connectivity.
 */
import React from "react";
import { render, screen, act, cleanup, fireEvent, within } from "@testing-library/react";
import BroadcastConsole from "./BroadcastConsole";

let mockPermissions;
let mockBroadcast;

jest.mock("react-router-dom", () => ({
  Link: ({ to, children, ...rest }) => <a href={to} {...rest}>{children}</a>,
}), { virtual: true });
jest.mock("@/contexts/BroadcastContext", () => ({
  useBroadcast: () => mockBroadcast,
}));
jest.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ can: (code) => mockPermissions.has(code) }),
}));
jest.mock("@/lib/api", () => ({
  api: { get: jest.fn(), post: jest.fn() },
  getToken: () => "t",
  wsUrl: (p) => `ws://localhost:8000${p}`,
}));

// eslint-disable-next-line import/first
const { api } = require("@/lib/api");

const ZONES = ["NORTH", "SOUTH", "WEST"];
const CITIES = ["DELHI", "MUMBAI", "JAIPUR"];

/** 25 Stores, so several pages exist at the default size of 10. */
function fleet() {
  return Array.from({ length: 25 }, (_, index) => ({
    id: index + 1,
    store_code: `S${String(index + 1).padStart(2, "0")}`,
    store_name: `Store ${index + 1}`,
    city: CITIES[index % CITIES.length],
    region: ZONES[index % ZONES.length],
    is_online_store: false,
    // Every third Store has a connected Receiver.
    status: index % 3 === 0 ? "online" : "offline",
  }));
}

/** A connected Store and an unconnected one.
 *
 * Deliberately synthetic names: the frontend must never embed the canonical
 * Store catalogue, and a guard in the backend suite enforces that.
 */
const BP = { id: 101, store_code: "BP", store_name: "Test Store North", city: "DELHI",
             region: "UN ZONE", is_online_store: false, status: "online" };
const RG = { id: 102, store_code: "RG", store_name: "Test Store South",
             city: "DELHI", region: "UN ZONE", is_online_store: false,
             status: "offline" };
//: An e-commerce Store: flagged Online in Store Management, no Receiver.
const WEB = { id: 103, store_code: "WEB", store_name: "Web Store", city: "DELHI",
              region: "UN ZONE", is_online_store: true, status: "offline" };

function targetsBody(stores) {
  return {
    stores,
    regions: Array.from(new Set(stores.map((s) => s.region))).sort(),
    cities: Array.from(new Set(stores.map((s) => s.city))).sort(),
  };
}

async function renderConsole(stores = fleet(),
                             permissions = ["broadcast.start", "broadcast.stop",
                                            "broadcast.store_delivery"]) {
  mockPermissions = new Set(permissions);
  api.get.mockImplementation(async () => ({ data: targetsBody(stores) }));
  render(<BroadcastConsole />);
  await act(async () => {});
}

async function setMode(value) {
  await act(async () => {
    fireEvent.change(screen.getByTestId("target-mode-select"), { target: { value } });
  });
}

function visibleCodes() {
  return Array.from(document.querySelectorAll('[data-testid^="store-row-"]'))
    .map((row) => row.getAttribute("data-testid").replace("store-row-", ""));
}

beforeEach(() => {
  jest.clearAllMocks();
  mockBroadcast = {
    current: { live: false },
    load: jest.fn(async () => {}),
    isLive: false, meter: 0, micLevels: null, broadcasterStatus: "idle",
    micVolumePercent: 100, micMuted: false,
    setMicVolume: jest.fn(), setMicMute: jest.fn(),
    micEffectivelySilent: false, error: "", setError: jest.fn(),
    startBroadcast: jest.fn(), stopBroadcast: jest.fn(), emergencyStop: jest.fn(),
    active: null, isStoreBusyForOthers: () => false,
  };
  api.get.mockImplementation(async () => ({ data: targetsBody([]) }));
  api.post.mockImplementation(async () => ({ data: { id: 1 } }));
});

afterEach(cleanup);

// ===========================================================================
// Online Stores Only reads connectivity
// ===========================================================================

test("Online Stores Only counts the connected Store, not the e-commerce one", async () => {
  // The exact defect: BP is a PHYSICAL shop with a connected Receiver, WEB is
  // an e-commerce Store with none.
  await renderConsole([BP, RG, WEB]);
  await setMode("online_only");

  expect(screen.getByTestId("stat-selected").textContent).toContain("1");
  expect(screen.getByTestId("stat-online").textContent).toContain("1");
});

test("the first card is TARGETS, not SELECTED", async () => {
  // "Selected 0" beside an automatic mode reads as a fault, because nobody
  // selected anything.
  await renderConsole([BP, RG, WEB]);
  await setMode("online_only");
  expect(screen.getByTestId("stat-selected").textContent).toMatch(/targets/i);
});

test("an offline Store is reported as excluded, not as a target", async () => {
  await renderConsole([BP, RG, WEB]);
  await setMode("online_only");
  // RG and WEB both lack a connected Receiver.
  expect(screen.getByTestId("stat-offline").textContent).toContain("2");
  expect(screen.getByTestId("stat-offline").textContent).toMatch(/excluded/i);
});

test("manual checkboxes cannot alter an automatic mode", async () => {
  await renderConsole([BP, RG, WEB]);
  // Tick RG while in Selected mode...
  await act(async () => {
    fireEvent.click(screen.getByTestId("store-checkbox-RG"));
  });
  expect(screen.getByTestId("stat-selected").textContent).toContain("1");

  // ...then switch. The draft must not narrow or widen the online set.
  await setMode("online_only");
  expect(screen.getByTestId("stat-selected").textContent).toContain("1");
  expect(screen.getByTestId("stat-online").textContent).toContain("1");
});

test("a mode with nothing online reports zero targets truthfully", async () => {
  await renderConsole([RG, WEB]);          // neither has a Receiver
  await setMode("online_only");
  expect(screen.getByTestId("stat-selected").textContent).toContain("0");
  expect(screen.getByTestId("start-broadcast-btn").disabled).toBe(true);
});

test("switching modes does not leak a draft selection", async () => {
  await renderConsole([BP, RG, WEB]);
  await act(async () => { fireEvent.click(screen.getByTestId("store-checkbox-RG")); });

  await setMode("region");
  await setMode(ONLY_WITH_LINK_VALUE);
  // Link-only is zero physical targets whatever was ticked.
  expect(screen.getByTestId("stat-selected").textContent).toContain("0");

  await setMode("selected");
  // Returning to Selected keeps the operator's draft, which is theirs.
  expect(screen.getByTestId("stat-selected").textContent).toContain("1");
});
const ONLY_WITH_LINK_VALUE = "only_with_link";

test("Only With Link hides the Store picker entirely", async () => {
  await renderConsole([BP, RG, WEB]);
  await setMode(ONLY_WITH_LINK_VALUE);
  expect(screen.queryByTestId("stores-search")).toBeNull();
  expect(screen.queryByTestId("stores-page-info")).toBeNull();
});

test("without physical delivery no Store inventory is ever requested", async () => {
  await renderConsole(fleet(), ["broadcast.start", "broadcast.stop"]);
  const asked = api.get.mock.calls.map(([url]) => url);
  expect(asked).not.toContain("/broadcast/target-stores");
  expect(screen.queryByTestId("stores-search")).toBeNull();
});

// ===========================================================================
// Pagination
// ===========================================================================

test("only one page of Stores is rendered", async () => {
  await renderConsole();
  expect(visibleCodes().length).toBe(10);
  expect(screen.getByTestId("stores-page-info").textContent).toMatch(/Page 1 of 3/);
  expect(screen.getByTestId("stores-result-count").textContent).toContain("of 25");
});

test("Next and Previous move through the fleet", async () => {
  await renderConsole();
  const first = visibleCodes();

  await act(async () => { fireEvent.click(screen.getByTestId("stores-next-page")); });
  const second = visibleCodes();
  expect(second).not.toEqual(first);
  expect(second.length).toBe(10);
  expect(screen.getByTestId("stores-page-info").textContent).toMatch(/Page 2 of 3/);

  await act(async () => { fireEvent.click(screen.getByTestId("stores-prev-page")); });
  expect(visibleCodes()).toEqual(first);
});

test("the last page holds the remainder and Next stops there", async () => {
  await renderConsole();
  await act(async () => { fireEvent.click(screen.getByTestId("stores-next-page")); });
  await act(async () => { fireEvent.click(screen.getByTestId("stores-next-page")); });
  expect(visibleCodes().length).toBe(5);
  expect(screen.getByTestId("stores-next-page").disabled).toBe(true);
});

test("the page size can be changed", async () => {
  await renderConsole();
  await act(async () => {
    fireEvent.change(screen.getByTestId("stores-page-size"), { target: { value: "20" } });
  });
  expect(visibleCodes().length).toBe(20);
  expect(screen.getByTestId("stores-page-info").textContent).toMatch(/Page 1 of 2/);
});

test("no Store appears twice across pages", async () => {
  await renderConsole();
  const seen = [];
  seen.push(...visibleCodes());
  await act(async () => { fireEvent.click(screen.getByTestId("stores-next-page")); });
  seen.push(...visibleCodes());
  await act(async () => { fireEvent.click(screen.getByTestId("stores-next-page")); });
  seen.push(...visibleCodes());

  expect(seen.length).toBe(25);
  expect(new Set(seen).size).toBe(25);
});

// ===========================================================================
// Filtering
// ===========================================================================

test("search matches code, name, city and Zone", async () => {
  await renderConsole([BP, RG, WEB, ...fleet()]);

  for (const [term, expected] of [["BP", "BP"], ["Test Store North", "BP"],
                                  ["south", "RG"]]) {
    await act(async () => {
      fireEvent.change(screen.getByTestId("stores-search"), { target: { value: term } });
    });
    expect(visibleCodes()).toContain(expected);
  }

  // Zone and city are searchable too, which is what an operator typing
  // "UN ZONE" means.
  await act(async () => {
    fireEvent.change(screen.getByTestId("stores-search"), { target: { value: "UN ZONE" } });
  });
  expect(visibleCodes().sort()).toEqual(["BP", "RG", "WEB"]);
});

test("the Zone filter narrows the rows", async () => {
  await renderConsole();
  await act(async () => { fireEvent.click(screen.getByTestId("stores-filter-zone")); });
  await act(async () => {
    fireEvent.click(screen.getByTestId("stores-filter-zone-option-NORTH"));
  });
  const rows = visibleCodes();
  expect(rows.length).toBeGreaterThan(0);
  rows.forEach((code) => {
    const index = Number(code.slice(1)) - 1;
    expect(ZONES[index % ZONES.length]).toBe("NORTH");
  });
});

test("the City filter narrows the rows", async () => {
  await renderConsole();
  await act(async () => { fireEvent.click(screen.getByTestId("stores-filter-city")); });
  await act(async () => {
    fireEvent.click(screen.getByTestId("stores-filter-city-option-MUMBAI"));
  });
  expect(visibleCodes().length).toBeGreaterThan(0);
  expect(screen.getByTestId("stores-result-count").textContent).toContain("authorised");
});

test("the Status filter separates connected from not", async () => {
  await renderConsole([BP, RG, WEB]);
  await act(async () => { fireEvent.click(screen.getByTestId("stores-filter-status")); });
  await act(async () => {
    fireEvent.click(screen.getByTestId("stores-filter-status-option-online"));
  });
  expect(visibleCodes()).toEqual(["BP"]);

  // The panel stays open, and the filter now ADDS rather than replaces - so
  // moving from one value to the other means unticking the first. That is the
  // behaviour: ticking both would legitimately show every Store.
  await act(async () => {
    fireEvent.click(screen.getByTestId("stores-filter-status-option-online"));
  });
  await act(async () => {
    fireEvent.click(screen.getByTestId("stores-filter-status-option-offline"));
  });
  // WEB is flagged "Online" in Store Management but has no Receiver.
  expect(visibleCodes().sort()).toEqual(["RG", "WEB"]);
});

test("filters combine", async () => {
  await renderConsole();
  await act(async () => { fireEvent.click(screen.getByTestId("stores-filter-zone")); });
  await act(async () => {
    fireEvent.click(screen.getByTestId("stores-filter-zone-option-NORTH"));
  });
  await act(async () => { fireEvent.click(screen.getByTestId("stores-filter-status")); });
  await act(async () => {
    fireEvent.click(screen.getByTestId("stores-filter-status-option-online"));
  });
  visibleCodes().forEach((code) => {
    const index = Number(code.slice(1)) - 1;
    expect(ZONES[index % ZONES.length]).toBe("NORTH");
    expect(index % 3).toBe(0);
  });
});

test("filtering returns to page one", async () => {
  await renderConsole();
  await act(async () => { fireEvent.click(screen.getByTestId("stores-next-page")); });
  expect(screen.getByTestId("stores-page-info").textContent).toMatch(/Page 2/);

  await act(async () => { fireEvent.click(screen.getByTestId("stores-filter-zone")); });
  await act(async () => {
    fireEvent.click(screen.getByTestId("stores-filter-zone-option-SOUTH"));
  });
  // Staying on page 2 of a smaller result set would show an empty table.
  expect(screen.getByTestId("stores-page-info").textContent).toMatch(/Page 1/);
});

test("a filter that matches nothing says so", async () => {
  await renderConsole();
  await act(async () => {
    fireEvent.change(screen.getByTestId("stores-search"), { target: { value: "zzzz" } });
  });
  expect(screen.getByTestId("stores-result-count").textContent).toMatch(/No Stores match/i);
  expect(visibleCodes().length).toBe(0);
});

test("the Zone FILTER does not change targeting", async () => {
  // Filtering by a Zone changes which rows are visible. It must never become
  // Zone TARGET MODE.
  await renderConsole();
  await act(async () => { fireEvent.click(screen.getByTestId("store-checkbox-S01")); });
  await act(async () => { fireEvent.click(screen.getByTestId("stores-filter-zone")); });
  await act(async () => {
    fireEvent.click(screen.getByTestId("stores-filter-zone-option-NORTH"));
  });
  expect(screen.getByTestId("stat-selected").textContent).toContain("1");
  expect(screen.getByTestId("target-mode-select").value).toBe("selected");
});

// ===========================================================================
// Selection survives everything except Clear
// ===========================================================================

test("a selection made on page one survives going to page two and back", async () => {
  await renderConsole();
  await act(async () => { fireEvent.click(screen.getByTestId("store-checkbox-S01")); });

  await act(async () => { fireEvent.click(screen.getByTestId("stores-next-page")); });
  await act(async () => { fireEvent.click(screen.getByTestId("store-checkbox-S11")); });
  expect(screen.getByTestId("stat-selected").textContent).toContain("2");

  await act(async () => { fireEvent.click(screen.getByTestId("stores-prev-page")); });
  expect(screen.getByTestId("store-checkbox-S01").checked).toBe(true);
  expect(screen.getByTestId("stat-selected").textContent).toContain("2");
});

test("a selected Store hidden by a filter is still selected", async () => {
  await renderConsole();
  await act(async () => { fireEvent.click(screen.getByTestId("store-checkbox-S01")); });

  await act(async () => {
    fireEvent.change(screen.getByTestId("stores-search"), { target: { value: "Store 2" } });
  });
  expect(screen.queryByTestId("store-checkbox-S01")).toBeNull();
  // The target set belongs to the broadcast, not to the visible page.
  expect(screen.getByTestId("stores-selected-count").textContent).toContain("1");

  await act(async () => { fireEvent.click(screen.getByTestId("stores-clear-filters")); });
  expect(screen.getByTestId("store-checkbox-S01").checked).toBe(true);
});

test("clearing filters keeps the selection, clearing the selection removes it", async () => {
  await renderConsole();
  await act(async () => { fireEvent.click(screen.getByTestId("store-checkbox-S01")); });
  await act(async () => { fireEvent.click(screen.getByTestId("stores-clear-filters")); });
  expect(screen.getByTestId("stat-selected").textContent).toContain("1");

  await act(async () => { fireEvent.click(screen.getByTestId("clear-selection-btn")); });
  expect(screen.getByTestId("stat-selected").textContent).toContain("0");
});

test("Select page takes only the visible rows", async () => {
  await renderConsole();
  await act(async () => { fireEvent.click(screen.getByTestId("select-page-btn")); });
  // Ten, not twenty-five.
  expect(screen.getByTestId("stat-selected").textContent).toContain("10");
});

test("Select all filtered takes every match across pages and says how many", async () => {
  await renderConsole();
  // Unfiltered, so the match set genuinely spans more than the visible page.
  const button = screen.getByTestId("select-all-filtered-btn");
  const expected = Number(button.textContent.match(/\d+/)[0]);
  expect(expected).toBe(25);
  expect(expected).toBeGreaterThan(visibleCodes().length);

  await act(async () => { fireEvent.click(button); });
  expect(screen.getByTestId("stat-selected").textContent).toContain("25");
});

test("Select all filtered respects the active filter", async () => {
  await renderConsole();
  await act(async () => { fireEvent.click(screen.getByTestId("stores-filter-zone")); });
  await act(async () => {
    fireEvent.click(screen.getByTestId("stores-filter-zone-option-NORTH"));
  });
  const button = screen.getByTestId("select-all-filtered-btn");
  const expected = Number(button.textContent.match(/\d+/)[0]);

  await act(async () => { fireEvent.click(button); });
  // Exactly the filtered matches - never the whole fleet.
  expect(screen.getByTestId("stat-selected").textContent).toContain(String(expected));
  expect(expected).toBeLessThan(25);
});

test("Select page adds to an existing selection rather than replacing it", async () => {
  await renderConsole();
  await act(async () => { fireEvent.click(screen.getByTestId("stores-next-page")); });
  await act(async () => { fireEvent.click(screen.getByTestId("store-checkbox-S11")); });

  await act(async () => { fireEvent.click(screen.getByTestId("stores-prev-page")); });
  await act(async () => { fireEvent.click(screen.getByTestId("select-page-btn")); });
  // Ten from this page plus the one already chosen on the other.
  expect(screen.getByTestId("stat-selected").textContent).toContain("11");
});

test("refreshing the inventory does not discard the selection", async () => {
  await renderConsole();
  await act(async () => { fireEvent.click(screen.getByTestId("store-checkbox-S01")); });

  // A Store going offline is a status change, not a reason to deselect it.
  const updated = fleet().map((store) =>
    store.store_code === "S01" ? { ...store, status: "offline" } : store);
  api.get.mockImplementation(async () => ({ data: targetsBody(updated) }));
  await act(async () => { await mockBroadcast.load(); });
  await act(async () => {});

  expect(screen.getByTestId("stat-selected").textContent).toContain("1");
});


// ===========================================================================
// The target picker takes several zones at once
//
// This is where naming more than one matters most. A campaign is almost never
// one zone, and with a single-value filter the operator had to tick shops from
// one zone, change the filter, and trust that the first lot were still
// selected - which is exactly the moment somebody stops trusting the screen.
// ===========================================================================

test("two zones can be filtered to at once", async () => {
  await renderConsole();

  await act(async () => { fireEvent.click(screen.getByTestId("stores-filter-zone")); });
  await act(async () => {
    fireEvent.click(screen.getByTestId("stores-filter-zone-option-NORTH"));
  });
  await act(async () => {
    fireEvent.click(screen.getByTestId("stores-filter-zone-option-SOUTH"));
  });

  const zones = new Set(visibleCodes().map((code) => {
    const index = Number(code.slice(1)) - 1;
    return ZONES[index % ZONES.length];
  }));
  expect(zones).toEqual(new Set(["NORTH", "SOUTH"]));
  expect(screen.getByTestId("stores-filter-zone").textContent)
    .toContain("2 selected");
});

test("a selection survives a filter change", async () => {
  // The reason the single-value filter was painful: tick shops in one zone,
  // switch the filter, and the earlier ticks have to still be there.
  await renderConsole();

  await act(async () => { fireEvent.click(screen.getByTestId("stores-filter-zone")); });
  await act(async () => {
    fireEvent.click(screen.getByTestId("stores-filter-zone-option-NORTH"));
  });
  const firstCode = visibleCodes()[0];
  await act(async () => {
    fireEvent.click(screen.getByTestId(`store-checkbox-${firstCode}`));
  });

  await act(async () => {
    fireEvent.click(screen.getByTestId("stores-filter-zone-option-NORTH"));
  });
  await act(async () => {
    fireEvent.click(screen.getByTestId("stores-filter-zone-option-SOUTH"));
  });
  await act(async () => {
    fireEvent.click(screen.getByTestId("stores-filter-zone-option-NORTH"));
  });

  expect(screen.getByTestId(`store-checkbox-${firstCode}`).checked).toBe(true);
});


// ===========================================================================
// Sorting the picker
//
// Deliberately in the browser, unlike everywhere else. The other tables hold
// one page of a longer list, so sorting locally would order fifty rows while
// claiming to order three hundred. This one holds every Store the account can
// see, already in memory - so ordering it here orders all of it.
// ===========================================================================

test("the picker can be sorted by a column, and back again", async () => {
  await renderConsole();
  const codesBefore = visibleCodes();

  await act(async () => { fireEvent.click(screen.getByTestId("picker-sort-store_name")); });
  const ascending = visibleCodes();

  await act(async () => { fireEvent.click(screen.getByTestId("picker-sort-store_name")); });
  const descending = visibleCodes();
  expect(descending).not.toEqual(ascending);

  // Third click restores the list's own order, so there is a way back.
  await act(async () => { fireEvent.click(screen.getByTestId("picker-sort-store_name")); });
  expect(visibleCodes()).toEqual(codesBefore);
});

test("sorting does not disturb what is selected", async () => {
  // The selection belongs to the broadcast, not to the order the table
  // happens to be in.
  await renderConsole();
  const first = visibleCodes()[0];
  await act(async () => {
    fireEvent.click(screen.getByTestId(`store-checkbox-${first}`));
  });

  await act(async () => { fireEvent.click(screen.getByTestId("picker-sort-store_name")); });
  expect(screen.getByTestId(`store-checkbox-${first}`).checked).toBe(true);
});
