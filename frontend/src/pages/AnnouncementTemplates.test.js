/**
 * Templates: the plan, on its own page.
 *
 * What is worth holding here:
 *
 *   * a line names a zone, one Store, or several - and "several" is expanded
 *     into one line per Store rather than becoming a third shape the server
 *     has to resolve;
 *   * choosing several Stores needs no modifier key. A <select multiple>
 *     replaced the selection on a plain click, so choosing a fifth shop looked
 *     like it deselected the other four;
 *   * "Select All Filtered" is not a list of ids. The browser holds one page,
 *     so it sends the FILTERS and lets the server resolve them.
 */
import React from "react";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import AnnouncementTemplates from "./AnnouncementTemplates";
import { api } from "@/lib/api";

jest.mock("@/lib/api", () => ({
  api: { get: jest.fn(), post: jest.fn(), delete: jest.fn() },
}));

const permissions = {
  current: ["menu.announcements.view", "announcements.control",
            "announcements.templates.manage", "announcements.delete_permanently"],
};
jest.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ can: (code) => permissions.current.includes(code) }),
}));

const TEMPLATE = {
  id: 3, name: "Festival", description: "Diwali", is_live: true,
  window: "live - no end date",
  items: [{ audio_id: 7, zone: "NORTH", audio_title: "Diwali Offer" }],
};
const AUDIO = { id: 7, title: "Diwali Offer", original_filename: "d.mp3",
                byte_size: 1024 };
const STORES = [{ id: 4, store_name: "Nehru Place", store_code: "NA" },
                { id: 5, store_name: "Dwarka Mor", store_code: "DM" }];

function listResponse(items, total = null) {
  return { data: { items, total: total ?? items.length, page: 1, pages: 1,
                   has_more: false } };
}

beforeEach(() => {
  permissions.current = ["menu.announcements.view", "announcements.control",
                         "announcements.templates.manage",
                         "announcements.delete_permanently"];
  api.get.mockReset();
  api.post.mockReset();
  api.delete.mockReset();
  api.get.mockImplementation((path) => {
    if (path.startsWith("/announcements/templates")) return Promise.resolve(listResponse([TEMPLATE]));
    if (path.startsWith("/announcements/audio")) return Promise.resolve(listResponse([AUDIO]));
    if (path.startsWith("/receivers/filter-options")) {
      return Promise.resolve({ data: { stores: STORES } });
    }
    return Promise.resolve(listResponse([]));
  });
});

test("the page searches, filters and pages on the server", async () => {
  render(<AnnouncementTemplates />);
  await screen.findByTestId("template-row-3");

  expect(screen.getByTestId("templates-search")).toBeTruthy();
  expect(screen.getByTestId("templates-zone")).toBeTruthy();
  expect(screen.getByTestId("templates-status")).toBeTruthy();

  fireEvent.change(screen.getByTestId("templates-search"),
                   { target: { value: "festival" } });
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(
    "/announcements/templates", expect.objectContaining({
      params: expect.objectContaining({ q: "festival" }) })));
});

test("a template says why it is not playing", async () => {
  api.get.mockImplementation((path) => {
    if (path.startsWith("/announcements/templates")) {
      return Promise.resolve(listResponse([{ ...TEMPLATE, is_live: false,
                                             window: "expired 2026-01-01" }]));
    }
    return Promise.resolve(listResponse([]));
  });
  render(<AnnouncementTemplates />);
  const window_ = await screen.findByTestId("template-window-3");
  expect(window_.textContent).toMatch(/expired/i);
  expect(screen.getByTestId("template-play-3").disabled).toBe(true);
});

test("several Stores needs no modifier key and each becomes its own line", async () => {
  api.post.mockResolvedValue({ data: { id: 1 } });
  render(<AnnouncementTemplates />);
  await screen.findByTestId("template-row-3");
  fireEvent.click(screen.getByTestId("template-new"));

  fireEvent.change(screen.getByTestId("template-name"),
                   { target: { value: "Two shops" } });
  fireEvent.change(await screen.findByTestId("template-line-audio-0"),
                   { target: { value: "7" } });
  fireEvent.change(screen.getByTestId("template-line-target-0"),
                   { target: { value: "stores" } });

  fireEvent.click(await screen.findByTestId("template-line-store-0-4"));
  fireEvent.click(screen.getByTestId("template-line-store-0-5"));
  expect(screen.getByTestId("template-line-stores-0").textContent)
    .toContain("2 of 2 chosen");

  // A second click removes one rather than replacing the lot.
  fireEvent.click(screen.getByTestId("template-line-store-0-4"));
  expect(screen.getByTestId("template-line-stores-0").textContent)
    .toContain("1 of 2 chosen");
  fireEvent.click(screen.getByTestId("template-line-store-0-4"));

  fireEvent.click(screen.getByTestId("template-save"));
  await waitFor(() => expect(api.post).toHaveBeenCalled());
  const [, payload] = api.post.mock.calls[0];
  expect(payload.items).toEqual([
    { audio_id: 7, volume_percent: 80, store_id: 5 },
    { audio_id: 7, volume_percent: 80, store_id: 4 },
  ]);
});

test("a template with no complete line is refused before it is sent", async () => {
  render(<AnnouncementTemplates />);
  await screen.findByTestId("template-row-3");
  fireEvent.click(screen.getByTestId("template-new"));
  fireEvent.submit(await screen.findByTestId("template-builder"));

  expect(await screen.findByTestId("template-builder-error")).toBeTruthy();
  expect(api.post).not.toHaveBeenCalled();
});

test("Select All Filtered sends the filters, not a page of ids", async () => {
  api.post.mockResolvedValue({ data: { affected: 184 } });
  render(<AnnouncementTemplates />);
  await screen.findByTestId("template-row-3");

  fireEvent.click(screen.getByTestId("templates-select-all"));
  fireEvent.click(screen.getByTestId("templates-bulk-archive"));

  await waitFor(() => expect(api.post).toHaveBeenCalledWith(
    "/announcements/templates/archive",
    expect.objectContaining({ mode: "filtered" })));
});

test("selecting a page sends exactly those ids", async () => {
  api.post.mockResolvedValue({ data: { affected: 1 } });
  render(<AnnouncementTemplates />);
  await screen.findByTestId("template-row-3");

  fireEvent.click(screen.getByTestId("templates-select-page"));
  expect(screen.getByTestId("templates-chosen").textContent).toContain("1 selected");
  fireEvent.click(screen.getByTestId("templates-bulk-archive"));

  await waitFor(() => expect(api.post).toHaveBeenCalledWith(
    "/announcements/templates/archive", { mode: "ids", ids: [3] }));
});

test("bulk deletion asks for the word once, with the count in the sentence", async () => {
  api.post.mockResolvedValue({ data: { affected: 1 } });
  render(<AnnouncementTemplates />);
  await screen.findByTestId("template-row-3");

  fireEvent.click(screen.getByTestId("template-select-3"));
  fireEvent.click(screen.getByTestId("templates-bulk-delete"));

  const confirm = await screen.findByTestId("templates-delete-confirm");
  expect(confirm.textContent).toContain("1");
  const button = screen.getByTestId("templates-delete-confirm-btn");
  expect(button.disabled).toBe(true);

  fireEvent.change(screen.getByTestId("templates-delete-word"),
                   { target: { value: "DELETE" } });
  fireEvent.click(button);
  await waitFor(() => expect(api.post).toHaveBeenCalledWith(
    "/announcements/templates/delete",
    { mode: "ids", ids: [3], confirm: "DELETE" }));
});

test("an account without the delete right is not shown it", async () => {
  permissions.current = ["menu.announcements.view", "announcements.templates.manage"];
  render(<AnnouncementTemplates />);
  await screen.findByTestId("template-row-3");

  expect(screen.queryByTestId("templates-bulk-delete")).toBeNull();
  expect(screen.queryByTestId("template-delete-3")).toBeNull();
  expect(screen.getByTestId("template-archive-3")).toBeTruthy();
});

test("archiving reports what it did to the shops running it", async () => {
  api.delete.mockResolvedValue({ data: {
    ok: true, stopped_stores: [4],
    note: "Archived. 1 Store(s) were playing it and have been stopped." } });

  render(<AnnouncementTemplates />);
  fireEvent.click(await screen.findByTestId("template-archive-3"));

  const note = await screen.findByTestId("templates-note");
  expect(note.textContent).toMatch(/have been stopped/i);
});


// ===========================================================================
// A filter may name more than one value
//
// One Store answers "what is scheduled for Nehru Place". It cannot answer
// "what is scheduled for these six", which is the question people bring - a
// zone with an exception in it is the normal case, and neither a zone filter
// nor a single Store describes it.
// ===========================================================================

test("templates can be filtered by Store, and by several Stores", async () => {
  render(<AnnouncementTemplates />);
  await screen.findByTestId("template-row-3");

  fireEvent.click(screen.getByTestId("templates-store"));
  fireEvent.click(await screen.findByTestId("templates-store-option-4"));
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(
    "/announcements/templates", expect.objectContaining({
      params: expect.objectContaining({ store_id: "4" }) })));

  fireEvent.click(screen.getByTestId("templates-store-option-5"));
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(
    "/announcements/templates", expect.objectContaining({
      params: expect.objectContaining({ store_id: "4,5" }) })));
});

test("choosing a second value adds to the first rather than replacing it", async () => {
  render(<AnnouncementTemplates />);
  await screen.findByTestId("template-row-3");

  fireEvent.click(screen.getByTestId("templates-store"));
  fireEvent.click(await screen.findByTestId("templates-store-option-4"));
  fireEvent.click(screen.getByTestId("templates-store-option-5"));
  expect(screen.getByTestId("templates-store").textContent).toContain("2 selected");

  // And clicking a chosen one removes it.
  fireEvent.click(screen.getByTestId("templates-store-option-4"));
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(
    "/announcements/templates", expect.objectContaining({
      params: expect.objectContaining({ store_id: "5" }) })));
});

test("the All option clears every chosen value", async () => {
  render(<AnnouncementTemplates />);
  await screen.findByTestId("template-row-3");

  fireEvent.click(screen.getByTestId("templates-store"));
  fireEvent.click(await screen.findByTestId("templates-store-option-4"));
  fireEvent.click(screen.getByTestId("templates-store-clear"));

  await waitFor(() => {
    const last = api.get.mock.calls.filter(
      ([path]) => path === "/announcements/templates").at(-1);
    expect(last[1].params.store_id).toBeUndefined();
  });
  expect(screen.getByTestId("templates-store").textContent).toContain("All Stores");
});
