/**
 * The Announcements page.
 *
 * The two things worth holding here are about attention, not correctness of
 * data:
 *
 *   * the play console and the setup screens are separate. On one page the
 *     second buries the first - somebody scrolls past a recordings list to
 *     reach a Pause button for a shop that is annoying customers right now.
 *   * DUCKED is shown as itself. A Store standing aside for a broadcast comes
 *     back on its own; one a person paused does not. Calling both "Paused"
 *     makes the console's behaviour look arbitrary.
 */
import React from "react";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import Announcements from "./Announcements";
import { api } from "@/lib/api";

jest.mock("@/lib/api", () => ({
  api: { get: jest.fn(), post: jest.fn(), delete: jest.fn() },
}));

const permissions = {
  current: ["menu.announcements.view", "announcements.control",
            "announcements.control_all", "announcements.volume",
            "announcements.upload", "announcements.templates.manage"],
};
jest.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ can: (code) => permissions.current.includes(code) }),
}));

const STORE = {
  store_id: 4, store_code: "NA", store_name: "Nehru Place", zone: "NORTH",
  state: "PLAYING", volume_percent: 70, template_name: "Diwali",
  audio_title: "Diwali Offer",
};

function listResponse(items) {
  return { data: { items, total: items.length, page: 1, pages: 1, has_more: false } };
}

beforeEach(() => {
  permissions.current = ["menu.announcements.view", "announcements.control",
                         "announcements.control_all", "announcements.volume",
                         "announcements.upload", "announcements.templates.manage"];
  api.get.mockReset();
  api.post.mockReset();
  api.delete.mockReset();
  api.get.mockImplementation((path) => {
    if (path.startsWith("/announcements/status")) return Promise.resolve(listResponse([STORE]));
    if (path.startsWith("/announcements/templates")) return Promise.resolve(listResponse([]));
    if (path.startsWith("/announcements/audio")) return Promise.resolve(listResponse([]));
    return Promise.resolve(listResponse([]));
  });
});

test("the play console opens first, without the setup screens", async () => {
  render(<Announcements />);
  await screen.findByTestId("announcement-row-4");

  // The console is here...
  expect(screen.getByTestId("announcements-play-all")).toBeTruthy();
  // ...and the things you set up once a fortnight are not in the way.
  expect(screen.queryByTestId("recording-upload-form")).toBeNull();
  expect(screen.queryByText(/No templates yet/i)).toBeNull();
});

test("setup is a separate place, reached deliberately", async () => {
  render(<Announcements />);
  await screen.findByTestId("announcement-row-4");

  fireEvent.click(screen.getByTestId("announcements-tab-setup"));
  await waitFor(() => expect(screen.getByTestId("recording-upload-form")).toBeTruthy());

  // And the console's controls are not duplicated there.
  expect(screen.queryByTestId("announcement-row-4")).toBeNull();
});

test("a Store standing aside for a broadcast is not called Paused", async () => {
  api.get.mockImplementation((path) => {
    if (path.startsWith("/announcements/status")) {
      return Promise.resolve(listResponse([{ ...STORE, state: "DUCKED" }]));
    }
    return Promise.resolve(listResponse([]));
  });

  render(<Announcements />);
  const badge = await screen.findByTestId("announcement-state-DUCKED");
  expect(badge.textContent).toBe("Broadcast");
  expect(badge.getAttribute("title")).toMatch(/resumes by itself/i);
});

test("a paused Store says it will NOT come back on its own", async () => {
  api.get.mockImplementation((path) => {
    if (path.startsWith("/announcements/status")) {
      return Promise.resolve(listResponse([{ ...STORE, state: "PAUSED" }]));
    }
    return Promise.resolve(listResponse([]));
  });

  render(<Announcements />);
  const badge = await screen.findByTestId("announcement-state-PAUSED");
  expect(badge.getAttribute("title")).toMatch(/will NOT come back/i);
});

test("the chosen recording is named, not left to a native button to imply", async () => {
  render(<Announcements />);
  await screen.findByTestId("announcement-row-4");
  fireEvent.click(screen.getByTestId("announcements-tab-setup"));

  const chosen = await screen.findByTestId("recording-chosen");
  expect(chosen.textContent).toMatch(/No file chosen yet/i);

  const input = screen.getByTestId("recording-file");
  fireEvent.change(input, {
    target: { files: [new File(["ID3"], "diwali.mp3", { type: "audio/mpeg" })] },
  });
  await waitFor(() => expect(screen.getByTestId("recording-chosen").textContent)
    .toContain("diwali.mp3"));
});

test("an account that may not reach the estate-wide buttons is not shown them", async () => {
  permissions.current = ["menu.announcements.view", "announcements.control"];
  render(<Announcements />);
  await screen.findByTestId("announcement-row-4");

  expect(screen.queryByTestId("announcements-play-all")).toBeNull();
  expect(screen.queryByTestId("announcements-pause-all")).toBeNull();
  // But the per-Store controls it DOES hold are there.
  expect(screen.getByTestId("announcement-pause-4")).toBeTruthy();
});

test("pausing one Store asks the server for exactly that Store", async () => {
  api.post.mockResolvedValue({ data: { state: "PAUSED" } });
  render(<Announcements />);
  fireEvent.click(await screen.findByTestId("announcement-pause-4"));

  await waitFor(() => expect(api.post)
    .toHaveBeenCalledWith("/announcements/stores/4/pause"));
});

test("a refusal from the server is shown rather than swallowed", async () => {
  api.post.mockRejectedValue({
    response: { data: { detail: "A live broadcast is playing in this Store." } } });
  render(<Announcements />);
  fireEvent.click(await screen.findByTestId("announcement-pause-4"));

  const failure = await screen.findByTestId("announcements-error");
  expect(failure.textContent).toContain("A live broadcast is playing in this Store.");
});
