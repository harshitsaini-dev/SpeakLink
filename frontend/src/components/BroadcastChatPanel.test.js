/**
 * The host's chat panel.
 *
 * Two properties are worth more than the rest here, and both are about NOT
 * deciding things the server has already decided:
 *
 *   * the private badge REPORTS what the server stored, it does not compute
 *     it - a panel that decided visibility client-side would be one refetch
 *     away from publishing somebody's private message;
 *   * a deleted message keeps its place and its author and loses its words,
 *     because everyone in the room already saw it.
 *
 * The third is that a message is rendered as TEXT. React does that by
 * construction; the test exists so that a future "render markdown" change has
 * to delete an assertion that says why not.
 */
import React from "react";
import { render, screen, act, cleanup, fireEvent, waitFor } from "@testing-library/react";
import BroadcastChatPanel from "./BroadcastChatPanel";

jest.mock("@/lib/api", () => ({
  api: { get: jest.fn(), post: jest.fn(), put: jest.fn() },
  API_BASE: "http://localhost:8000/api",
}));

// eslint-disable-next-line import/first
const { api } = require("@/lib/api");

const SESSION = 8;

function message(id, overrides = {}) {
  return {
    id, participant_id: 5, author_kind: "LISTENER", author_name: "Harshit",
    body: `message ${id}`, deleted: false, visibility: "PUBLIC",
    created_at: "2026-08-11T13:53:00Z", has_image: false, ...overrides,
  };
}

function serveChat(state) {
  api.get.mockImplementation((path) => {
    if (path.endsWith("/chat")) return Promise.resolve({ data: state });
    return Promise.resolve({ data: {} });
  });
}

async function renderPanel(state = {}) {
  serveChat({ chat_enabled: true, chat_mode: "PUBLIC", messages: [], ...state });
  render(<BroadcastChatPanel sessionId={SESSION} />);
  await act(async () => {});
}

beforeEach(() => {
  jest.clearAllMocks();
  api.post.mockResolvedValue({ data: {} });
  api.put.mockResolvedValue({ data: { chat_enabled: true, chat_mode: "PUBLIC", messages: [] } });
});

afterEach(cleanup);


test("an empty room says so rather than looking broken", async () => {
  await renderPanel();
  expect(screen.getByTestId("chat-empty")).toBeTruthy();
});

test("messages render in order with their author and time", async () => {
  await renderPanel({ messages: [message(1), message(2, { author_kind: "HOST" })] });
  expect(screen.getByTestId("chat-message-1").textContent).toContain("Harshit");
  expect(screen.getByTestId("chat-message-2").textContent).toContain("You");
  // Asia/Kolkata, whatever the machine running the test is set to.
  expect(screen.getByTestId("chat-time-1").textContent).toMatch(/\d{2}:\d{2} (AM|PM)/);
});

test("a message is rendered as text, never as markup", async () => {
  const payload = '<img src=x onerror="alert(1)">';
  await renderPanel({ messages: [message(1, { body: payload })] });
  const node = screen.getByTestId("chat-message-1");
  expect(node.textContent).toContain(payload);
  expect(node.querySelector("img")).toBeNull();
});

test("a private message is badged from what the server stored", async () => {
  await renderPanel({
    chat_mode: "PRIVATE",
    messages: [message(1, { visibility: "PRIVATE" }), message(2)],
  });
  expect(screen.getByTestId("chat-private-1")).toBeTruthy();
  expect(screen.queryByTestId("chat-private-2")).toBeNull();
});

test("a deleted message keeps its author and loses its words", async () => {
  await renderPanel({
    messages: [message(1, { deleted: true, body: null })],
  });
  expect(screen.getByTestId("chat-removed-1")).toBeTruthy();
  expect(screen.getByTestId("chat-message-1").textContent).toContain("Harshit");
  // And there is no Delete button for something already deleted.
  expect(screen.queryByTestId("chat-delete-1")).toBeNull();
});

test("sending a message posts it and re-reads the room", async () => {
  await renderPanel();
  fireEvent.change(screen.getByTestId("chat-input"),
                   { target: { value: "  saying it again  " } });
  await act(async () => { fireEvent.click(screen.getByTestId("chat-send")); });

  expect(api.post).toHaveBeenCalledWith(`/broadcast/sessions/${SESSION}/chat`,
                                        { body: "saying it again" });
  // Two reads: the mount and the one after sending. What was actually stored
  // is the server's answer, not what this panel assumed.
  expect(api.get.mock.calls.filter(([p]) => p.endsWith("/chat")).length)
    .toBeGreaterThanOrEqual(2);
});

test("turning chat off leaves the host able to reply", async () => {
  // The switch stops the AUDIENCE. An operator may still need to answer the
  // last question before the room goes quiet.
  await renderPanel({ chat_enabled: false });
  expect(screen.getByTestId("chat-off-badge")).toBeTruthy();
  expect(screen.getByTestId("chat-input").disabled).toBeFalsy();
});

test("the switches say what they do next, not what is true now", async () => {
  await renderPanel({ chat_enabled: true, chat_mode: "PUBLIC" });
  expect(screen.getByTestId("chat-toggle-enabled").textContent).toMatch(/Turn chat off/);
  expect(screen.getByTestId("chat-toggle-mode").textContent).toMatch(/Make private/);

  await act(async () => { fireEvent.click(screen.getByTestId("chat-toggle-mode")); });
  expect(api.put).toHaveBeenCalledWith(
    `/broadcast/sessions/${SESSION}/chat/settings`, { chat_mode: "PRIVATE" });
});

test("a refusal is shown rather than swallowed", async () => {
  await renderPanel();
  api.post.mockRejectedValueOnce({
    response: { data: { detail: "A message cannot be longer than 500 characters." } } });
  fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "x" } });
  await act(async () => { fireEvent.click(screen.getByTestId("chat-send")); });
  await waitFor(() => {
    expect(screen.getByTestId("chat-error").textContent).toMatch(/500 characters/);
  });
});

test("an image is sent as multipart with the typed caption", async () => {
  await renderPanel();
  fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "like this" } });
  const file = new File(["binary"], "photo.png", { type: "image/png" });
  await act(async () => {
    fireEvent.change(screen.getByTestId("chat-image-input"), { target: { files: [file] } });
  });

  const [path, form] = api.post.mock.calls[0];
  expect(path).toBe(`/broadcast/sessions/${SESSION}/chat/image`);
  expect(form.get("file")).toBe(file);
  expect(form.get("body")).toBe("like this");
});

test("an image message fetches its bytes through the API, not a bare src", async () => {
  // The bytes are behind the same permission as the message, so a plain
  // <img src> would arrive without the bearer token and 401.
  global.URL.createObjectURL = jest.fn(() => "blob:fake");
  global.URL.revokeObjectURL = jest.fn();
  api.get.mockImplementation((path) => {
    if (path.endsWith("/chat")) {
      return Promise.resolve({ data: {
        chat_enabled: true, chat_mode: "PUBLIC",
        messages: [message(1, { has_image: true, body: null })] } });
    }
    return Promise.resolve({ data: new Blob(["bytes"]) });
  });
  render(<BroadcastChatPanel sessionId={SESSION} />);
  await act(async () => {});

  await waitFor(() => expect(screen.getByTestId("chat-image-1")).toBeTruthy());
  expect(api.get).toHaveBeenCalledWith(
    `/broadcast/sessions/${SESSION}/chat/messages/1/image`,
    { responseType: "blob" });
});

test("searching narrows the messages, and says so when nothing matches", async () => {
  await renderPanel({ messages: [
    message(1, { body: "we cannot hear you" }),
    message(2, { body: "all fine here", author_name: "Priya" }),
  ] });

  fireEvent.change(screen.getByTestId("chat-search"), { target: { value: "hear" } });
  expect(screen.getByTestId("chat-message-1")).toBeTruthy();
  expect(screen.queryByTestId("chat-message-2")).toBeNull();

  fireEvent.change(screen.getByTestId("chat-search"), { target: { value: "zzz" } });
  // "No matches" is not "nobody has spoken" - an operator who read one as the
  // other would think the room had gone quiet.
  expect(screen.getByTestId("chat-no-matches")).toBeTruthy();
  expect(screen.queryByTestId("chat-empty")).toBeNull();

  fireEvent.click(screen.getByTestId("chat-clear-filter"));
  expect(screen.getByTestId("chat-message-2")).toBeTruthy();
});

test("the filter picks out one kind of message", async () => {
  await renderPanel({ messages: [
    message(1),
    message(2, { author_kind: "HOST", author_name: "superadmin" }),
  ] });

  fireEvent.change(screen.getByTestId("chat-filter"), { target: { value: "host" } });
  expect(screen.queryByTestId("chat-message-1")).toBeNull();
  expect(screen.getByTestId("chat-message-2")).toBeTruthy();
});
