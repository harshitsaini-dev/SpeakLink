/**
 * The announcement listening page.
 *
 * Everybody reading this page is somebody who has never seen the rest of the
 * product: they were sent a link. So what is tested is mostly what the page
 * SAYS - and the one thing it must never say is that this is a live feed of a
 * particular shop.
 */
import React from "react";
import { render, screen, waitFor, fireEvent, act } from "@testing-library/react";
import AnnounceListen from "./AnnounceListen";
import { api } from "@/lib/api";

jest.mock("@/lib/api", () => ({ api: { get: jest.fn(), post: jest.fn() } }));

const STATE = {
  label: "Diwali Offer", template_name: "Festival", playing: true, reason: "",
  audio: { id: 7, url: "/api/announce/audio/7", volume_percent: 80 },
  window: "live - no end date",
};

beforeEach(() => {
  api.get.mockReset();
  api.post.mockReset();
  sessionStorage.clear();
  window.HTMLMediaElement.prototype.play = jest.fn().mockResolvedValue();
  window.HTMLMediaElement.prototype.pause = jest.fn();
});

test("it asks for an ID and a password, and nothing else is required", async () => {
  render(<AnnounceListen />);
  expect(screen.getByTestId("announce-id")).toBeTruthy();
  expect(screen.getByTestId("announce-password")).toBeTruthy();
  // A name is offered, not demanded: whoever holds the link is not a user of
  // this product and must never need to become one.
  expect(screen.getByTestId("announce-name").required).toBe(false);
});

test("a refused ID or password is reported in the page's own words", async () => {
  api.post.mockRejectedValue({ response: { status: 401, data: {
    detail: "That listening ID or password is not right." } } });

  render(<AnnounceListen />);
  fireEvent.change(screen.getByTestId("announce-id"), { target: { value: "AN-XX" } });
  fireEvent.change(screen.getByTestId("announce-password"), { target: { value: "no" } });
  await act(async () => { fireEvent.click(screen.getByTestId("announce-join")); });

  expect((await screen.findByTestId("announce-error")).textContent)
    .toMatch(/not right/i);
});

test("once admitted it shows what is playing, and asks before starting audio", async () => {
  api.post.mockResolvedValue({ data: { token: "t", room: { public_code: "AN-A" } } });
  api.get.mockResolvedValue({ data: STATE });

  render(<AnnounceListen />);
  fireEvent.change(screen.getByTestId("announce-id"), { target: { value: "AN-A" } });
  fireEvent.change(screen.getByTestId("announce-password"), { target: { value: "p" } });
  await act(async () => { fireEvent.click(screen.getByTestId("announce-join")); });

  expect(await screen.findByTestId("announce-label")).toHaveProperty(
    "textContent", "Diwali Offer");
  // Browsers refuse to start audio before an interaction; saying so beats a
  // page that looks like it is playing and is not.
  expect(screen.getByTestId("announce-start")).toBeTruthy();
});

test("the page never claims to be a live feed of a shop", async () => {
  api.post.mockResolvedValue({ data: { token: "t", room: {} } });
  api.get.mockResolvedValue({ data: STATE });

  render(<AnnounceListen />);
  fireEvent.change(screen.getByTestId("announce-id"), { target: { value: "AN-A" } });
  fireEvent.change(screen.getByTestId("announce-password"), { target: { value: "p" } });
  await act(async () => { fireEvent.click(screen.getByTestId("announce-join")); });

  const page = (await screen.findByTestId("announce-listen-page")).textContent;
  expect(page).toMatch(/not a live feed/i);
  expect(page).toMatch(/a minute apart/i);
});

test("a paused campaign stops the page and says why", async () => {
  api.post.mockResolvedValue({ data: { token: "t", room: {} } });
  api.get.mockResolvedValue({ data: {
    ...STATE, playing: false, reason: "This announcement is paused right now." } });

  render(<AnnounceListen />);
  fireEvent.change(screen.getByTestId("announce-id"), { target: { value: "AN-A" } });
  fireEvent.change(screen.getByTestId("announce-password"), { target: { value: "p" } });
  await act(async () => { fireEvent.click(screen.getByTestId("announce-join")); });
  await act(async () => { fireEvent.click(await screen.findByTestId("announce-start")); });

  expect(screen.getByTestId("announce-status").textContent).toMatch(/paused/i);
  expect(window.HTMLMediaElement.prototype.pause).toHaveBeenCalled();
});

test("a closed link ends the session rather than looping on an error", async () => {
  // The token is dropped, so a reload shows the join form instead of looking
  // like a bug.
  sessionStorage.setItem("speaklink.announce.token", "stale");
  api.get.mockRejectedValue({ response: { status: 401, data: {
    detail: "This listening link is no longer open. Ask for a new one." } } });

  render(<AnnounceListen />);
  await waitFor(() => expect(screen.getByTestId("announce-join-page")).toBeTruthy());
  expect((await screen.findByTestId("announce-error")).textContent)
    .toMatch(/no longer open/i);
  expect(sessionStorage.getItem("speaklink.announce.token")).toBeNull();
});
