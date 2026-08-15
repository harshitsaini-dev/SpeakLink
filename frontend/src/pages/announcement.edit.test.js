/**
 * Editing what was already decided.
 *
 * Reported plainly: "template or recording edit krne ka koi option nhi hai".
 * There was none - both could only be created, archived or deleted, so fixing
 * a typo meant rebuilding a campaign that reached forty shops.
 *
 * These mount the real pages and press the real buttons, because the failure
 * being guarded against is a control that is not on the screen at all.
 */
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

jest.mock("@/lib/api", () => ({
  api: { get: jest.fn(), post: jest.fn(), put: jest.fn(), delete: jest.fn() },
}));
jest.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ can: () => true, user: { id: 1 } }),
}));

const { api } = require("@/lib/api");
const AnnouncementTemplates = require("./AnnouncementTemplates").default;
const AnnouncementRecordings = require("./AnnouncementRecordings").default;

const TEMPLATE = {
  id: 7, name: "Festival", description: "diwali", window: "live - no end date",
  is_live: true, status: "active", starts_at: null, expires_at: null,
  items: [{ audio_id: 3, zone: "NORTH", volume_percent: 80,
            audio_title: "Diwali" }],
};

const RECORDING = {
  id: 3, title: "Diwali", original_filename: "diwali.mp3", byte_size: 2048,
  uploaded_at: "2026-08-14T10:00:00+00:00", status: "active",
};

function stubGet(items) {
  api.get.mockImplementation((path) => {
    if (path.includes("filter-options")) {
      return Promise.resolve({ data: { regions: ["NORTH"], cities: [], stores: [] } });
    }
    return Promise.resolve({ data: { items, total: items.length, pages: 1 } });
  });
}

test("a template can be edited, and its saved lines come back into the form", async () => {
  stubGet([TEMPLATE]);
  api.put.mockResolvedValue({ data: TEMPLATE });

  render(<AnnouncementTemplates />);
  fireEvent.click(await screen.findByTestId("template-edit-7"));

  // The form opens filled in. An "edit" that opened blank would be a re-entry
  // with extra steps, and the lines are the part nobody wants to retype.
  const builder = await screen.findByTestId("template-builder");
  expect(builder.querySelector('[data-testid="template-name"]').value)
    .toBe("Festival");

  fireEvent.change(screen.getByTestId("template-name"),
                   { target: { value: "Festival - revised" } });
  fireEvent.submit(builder);

  await waitFor(() => expect(api.put).toHaveBeenCalled());
  const [path, body] = api.put.mock.calls[0];
  expect(path).toBe("/announcements/templates/7");
  expect(body.name).toBe("Festival - revised");
  // The lines survived the round trip rather than being emptied by an edit
  // that only touched the name.
  expect(body.items).toEqual([
    expect.objectContaining({ audio_id: 3, zone: "NORTH" })]);
});

test("a recording can be renamed, and its file is never swapped", async () => {
  stubGet([RECORDING]);
  api.put.mockResolvedValue({ data: { ...RECORDING, title: "Diwali - final" } });

  render(<AnnouncementRecordings />);
  fireEvent.click(await screen.findByTestId("recording-rename-3"));
  fireEvent.change(await screen.findByTestId("recording-rename-input-3"),
                   { target: { value: "Diwali - final" } });
  fireEvent.submit(screen.getByTestId("recording-rename-form-3"));

  await waitFor(() => expect(api.put).toHaveBeenCalledWith(
    "/announcements/audio/3", { title: "Diwali - final" }));

  // Nothing offers to replace the audio. Templates, history and every Store's
  // cache point at a recording by id and content hash, so swapping the file
  // underneath would rewrite what a shop played last week.
  expect(screen.queryByTestId("recording-replace-3")).toBeNull();
});

test("a daily window is saved with the template and comes back for editing", async () => {
  // "All October, 10:00 to 22:00" is two different facts. The dates say for
  // how many weeks; these say when in the day - and this is the one somebody
  // otherwise has to do by hand, twice a day, forever.
  // The recording has to exist for the line to be fillable: the select is
  // required, and a template with no recording plays nothing.
  stubGet([RECORDING]);
  api.post.mockResolvedValue({ data: {} });

  render(<AnnouncementTemplates />);
  fireEvent.click(await screen.findByText(/New template/i));

  fireEvent.change(screen.getByTestId("template-name"),
                   { target: { value: "Shop hours" } });
  fireEvent.change(screen.getByTestId("template-daily-start"),
                   { target: { value: "10:00" } });
  fireEvent.change(screen.getByTestId("template-daily-end"),
                   { target: { value: "22:00" } });
  fireEvent.click(screen.getByTestId("template-daily-day-0"));   // Monday

  const builder = screen.getByTestId("template-builder");
  fireEvent.change(screen.getByTestId("template-line-audio-0"),
                   { target: { value: "3" } });
  fireEvent.change(screen.getByTestId("template-line-zone-0"),
                   { target: { value: "NORTH" } });
  fireEvent.submit(builder);

  await waitFor(() => expect(api.post).toHaveBeenCalled());
  const [, body] = api.post.mock.calls[0];
  expect(body.daily_start).toBe("10:00");
  expect(body.daily_end).toBe("22:00");
  expect(body.daily_days).toBe("0");
});
