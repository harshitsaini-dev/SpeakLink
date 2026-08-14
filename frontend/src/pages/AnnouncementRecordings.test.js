/**
 * Recordings, on their own page.
 *
 * An estate running campaigns for a year has hundreds of these. As a section
 * on the console it could have no search, no filter and no pagination - a list
 * nobody can use.
 */
import React from "react";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import AnnouncementRecordings from "./AnnouncementRecordings";
import { api } from "@/lib/api";

jest.mock("@/lib/api", () => ({
  api: { get: jest.fn(), post: jest.fn(), delete: jest.fn() },
}));

const permissions = {
  current: ["menu.announcements.view", "announcements.upload",
            "announcements.delete_permanently"],
};
jest.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ can: (code) => permissions.current.includes(code) }),
}));

const RECORDING = { id: 7, title: "Diwali Offer", original_filename: "d.mp3",
                    byte_size: 20480, uploaded_at: "2026-08-14T10:00:00Z",
                    status: "active" };

function listResponse(items, total = null) {
  return { data: { items, total: total ?? items.length, page: 1, pages: 1,
                   has_more: false } };
}

beforeEach(() => {
  permissions.current = ["menu.announcements.view", "announcements.upload",
                         "announcements.delete_permanently"];
  api.get.mockReset();
  api.post.mockReset();
  api.delete.mockReset();
  api.get.mockResolvedValue(listResponse([RECORDING]));
});

test("the page searches and filters on the server", async () => {
  render(<AnnouncementRecordings />);
  await screen.findByTestId("recording-7");

  fireEvent.change(screen.getByTestId("recordings-search"),
                   { target: { value: "diwali" } });
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(
    "/announcements/audio", expect.objectContaining({
      params: expect.objectContaining({ q: "diwali" }) })));
});

test("the chosen file is named rather than left to a native button to imply", async () => {
  render(<AnnouncementRecordings />);
  await screen.findByTestId("recording-7");

  expect(screen.getByTestId("recording-chosen").textContent)
    .toMatch(/No file chosen yet/i);
  fireEvent.change(screen.getByTestId("recording-file"), {
    target: { files: [new File(["ID3"], "diwali.mp3", { type: "audio/mpeg" })] },
  });
  await waitFor(() => expect(screen.getByTestId("recording-chosen").textContent)
    .toContain("diwali.mp3"));
});

test("recordings can be selected and deleted together", async () => {
  api.post.mockResolvedValue({ data: { affected: 1, note: "1 deleted." } });
  render(<AnnouncementRecordings />);
  await screen.findByTestId("recording-7");

  fireEvent.click(screen.getByTestId("recording-select-7"));
  fireEvent.click(screen.getByTestId("recordings-bulk-delete"));
  fireEvent.change(await screen.findByTestId("recordings-delete-word"),
                   { target: { value: "DELETE" } });
  fireEvent.click(screen.getByTestId("recordings-delete-confirm-btn"));

  await waitFor(() => expect(api.post).toHaveBeenCalledWith(
    "/announcements/audio/delete", { mode: "ids", ids: [7], confirm: "DELETE" }));
});

test("the answer says which recordings were kept because a template uses them", async () => {
  api.post.mockResolvedValue({ data: {
    affected: 1, skipped: [{ id: 8, used_by: "Festival" }],
    note: "1 deleted. 1 kept because a template still uses them." } });

  render(<AnnouncementRecordings />);
  await screen.findByTestId("recording-7");
  fireEvent.click(screen.getByTestId("recordings-select-page"));
  fireEvent.click(screen.getByTestId("recordings-bulk-delete"));
  fireEvent.change(await screen.findByTestId("recordings-delete-word"),
                   { target: { value: "DELETE" } });
  fireEvent.click(screen.getByTestId("recordings-delete-confirm-btn"));

  const note = await screen.findByTestId("recordings-note");
  expect(note.textContent).toMatch(/kept because a template still uses them/i);
});

test("an account without the delete right is not shown it", async () => {
  permissions.current = ["menu.announcements.view", "announcements.upload"];
  render(<AnnouncementRecordings />);
  await screen.findByTestId("recording-7");

  expect(screen.queryByTestId("recordings-bulk-delete")).toBeNull();
  expect(screen.queryByTestId("recording-delete-7")).toBeNull();
  expect(screen.getByTestId("recording-archive-7")).toBeTruthy();
});

test("an account that may only look is offered no bulk bar at all", async () => {
  permissions.current = ["menu.announcements.view"];
  render(<AnnouncementRecordings />);
  await screen.findByTestId("recording-7");

  expect(screen.queryByTestId("recordings-bulk-bar")).toBeNull();
  expect(screen.queryByTestId("recording-upload-form")).toBeNull();
});
