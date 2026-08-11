/**
 * The listener's chat panel.
 *
 * What matters here is what the page does when it is TOLD NO. A listener who
 * has been muted, or whose room has chat turned off, must be able to see why
 * rather than typing into a box that silently swallows messages - and the
 * controls must be disabled, because offering an action the server will refuse
 * is a promise the page already knows it cannot keep.
 *
 * The panel never filters messages. Which of them this listener may see is
 * decided by the server, in the query; a client-side filter would be one
 * refetch away from showing somebody else's private message.
 */
import React from "react";
import { render, screen, act, cleanup, fireEvent, waitFor } from "@testing-library/react";
import ListenerChat from "./ListenerChat";

jest.mock("@/lib/api", () => ({
  api: { get: jest.fn(), post: jest.fn() },
  API_BASE: "http://hq.test:8000/api",
}));

// eslint-disable-next-line import/first
const { api } = require("@/lib/api");

const ME = 7;

function message(id, overrides = {}) {
  return {
    id, participant_id: ME, author_kind: "LISTENER", author_name: "Harshit",
    body: `message ${id}`, deleted: false, visibility: "PUBLIC",
    created_at: "2026-08-11T13:53:00Z", has_image: false, ...overrides,
  };
}

async function renderChat(state = {}) {
  api.get.mockResolvedValue({ data: {
    public_code: "SL-ABC123", chat_enabled: true, chat_mode: "PUBLIC",
    muted: false, me: ME, messages: [], ...state } });
  render(<ListenerChat />);
  await act(async () => {});
}

beforeEach(() => {
  jest.clearAllMocks();
  api.post.mockResolvedValue({ data: {} });
});

afterEach(cleanup);


test("nothing renders until the listener's own state is known", () => {
  // No flash of a chat box for somebody who may not be admitted at all.
  api.get.mockReturnValue(new Promise(() => {}));
  render(<ListenerChat />);
  expect(screen.queryByTestId("listener-chat")).toBeNull();
});

test("a message from the broadcaster is labelled as such", async () => {
  await renderChat({ messages: [
    message(1, { author_kind: "HOST", author_name: "superadmin", participant_id: null }),
    message(2),
  ] });
  expect(screen.getByTestId("listener-chat-message-1").textContent).toContain("Broadcaster");
  // Their own message says "You" rather than repeating their name back at them.
  expect(screen.getByTestId("listener-chat-message-2").textContent).toContain("You");
});

test("another listener's message keeps their name", async () => {
  await renderChat({ messages: [message(1, { participant_id: 99, author_name: "Priya" })] });
  expect(screen.getByTestId("listener-chat-message-1").textContent).toContain("Priya");
});

test("a message is rendered as text, never as markup", async () => {
  const payload = "<script>alert(1)</script>";
  await renderChat({ messages: [message(1, { body: payload })] });
  const node = screen.getByTestId("listener-chat-message-1");
  expect(node.textContent).toContain(payload);
  expect(node.querySelector("script")).toBeNull();
});

test("private mode is stated, so nobody types in the open by mistake", async () => {
  await renderChat({ chat_mode: "PRIVATE" });
  expect(screen.getByTestId("listener-chat-private")).toBeTruthy();
  expect(screen.getByTestId("listener-chat-empty").textContent)
    .toMatch(/only the broadcaster/i);
});

test("chat turned off disables the composer and says why", async () => {
  await renderChat({ chat_enabled: false });
  expect(screen.getByTestId("listener-chat-off")).toBeTruthy();
  expect(screen.getByTestId("listener-chat-input").disabled).toBe(true);
  expect(screen.getByTestId("listener-chat-send").disabled).toBe(true);
  expect(screen.getByTestId("listener-chat-attach").disabled).toBe(true);
});

test("a muted listener is told, and can still listen", async () => {
  await renderChat({ muted: true });
  expect(screen.getByTestId("listener-chat-muted").textContent)
    .toMatch(/muted you in this chat/i);
  expect(screen.getByTestId("listener-chat-input").disabled).toBe(true);
});

test("sending posts the trimmed message and re-reads", async () => {
  await renderChat();
  fireEvent.change(screen.getByTestId("listener-chat-input"),
                   { target: { value: "  we cannot hear you  " } });
  await act(async () => { fireEvent.click(screen.getByTestId("listener-chat-send")); });
  expect(api.post).toHaveBeenCalledWith("/listen/chat",
                                        { body: "we cannot hear you" });
});

test("the rate limit refusal is shown in the listener's own words", async () => {
  await renderChat();
  api.post.mockRejectedValueOnce({ response: { data: {
    detail: "Too many messages. Wait a few seconds and try again." } } });
  fireEvent.change(screen.getByTestId("listener-chat-input"), { target: { value: "hi" } });
  await act(async () => { fireEvent.click(screen.getByTestId("listener-chat-send")); });
  await waitFor(() => {
    expect(screen.getByTestId("listener-chat-error").textContent)
      .toMatch(/Wait a few seconds/);
  });
});

test("an image is fetched with the listener's cookie, straight from the API", async () => {
  // A listener session IS a cookie, which the browser attaches to an image
  // request by itself - so this one is a plain src, unlike the host panel.
  // The server still applies the visibility rule to the bytes.
  await renderChat({ messages: [message(1, { has_image: true, body: null })] });
  const image = screen.getByTestId("listener-chat-image-1");
  expect(image.getAttribute("src"))
    .toBe("http://hq.test:8000/api/listen/chat/messages/1/image");
});

test("an image is sent as multipart", async () => {
  await renderChat();
  const file = new File(["binary"], "shop.jpg", { type: "image/jpeg" });
  await act(async () => {
    fireEvent.change(screen.getByTestId("listener-chat-image-input"),
                     { target: { files: [file] } });
  });
  const [path, form] = api.post.mock.calls[0];
  expect(path).toBe("/listen/chat/image");
  expect(form.get("file")).toBe(file);
});
