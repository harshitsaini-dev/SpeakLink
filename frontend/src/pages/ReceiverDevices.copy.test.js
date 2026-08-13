/**
 * Copying a one-time secret off this page.
 *
 * There was no Copy button. The enrolment code sat in a monospace block with
 * nothing to click, so it was selected by hand and retyped - once four
 * characters short, then at full length with one character wrong. HQ cannot
 * tell either of those from a code that never existed: all three are a hash
 * matching nothing, and all three get the same generic refusal.
 *
 * The fallback is not a nicety. navigator.clipboard is undefined on plain HTTP
 * outside localhost, which is exactly how a Store reaches this HQ.
 */
import React from "react";
import { render, screen, act, cleanup, fireEvent, waitFor } from "@testing-library/react";
import ReceiverDevices from "./ReceiverDevices";

jest.mock("react-router-dom", () => ({
  useParams: () => ({ storeId: "15" }),
  Link: ({ to, children, ...rest }) => <a href={to} {...rest}>{children}</a>,
}), { virtual: true });
jest.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ can: () => true }),
}));
jest.mock("@/lib/api", () => ({
  api: { get: jest.fn(), post: jest.fn() },
}));

// eslint-disable-next-line import/first
const { api } = require("@/lib/api");

const CODE = "AbCdEfGhIjKlMnOpQrStUvWxYz012345";  // 32, like a real one

async function issueACode() {
  api.get.mockImplementation((path) => {
    if (path.includes("receiver-devices/roles")) {
      return Promise.resolve({ data: { devices: [], store: { store_code: "ASR" } } });
    }
    return Promise.resolve({ data: { codes: [], devices: [] } });
  });
  api.post.mockResolvedValue({
    data: { code: CODE, store_id: 15, expires_in_seconds: 900 } });

  render(<ReceiverDevices />);
  await act(async () => {});
  await act(async () => {
    fireEvent.click(screen.getByTestId("create-enrolment-code-btn"));
  });
}

beforeEach(() => {
  jest.clearAllMocks();
  Object.assign(navigator, { clipboard: { writeText: jest.fn(async () => {}) } });
});

afterEach(cleanup);

test("the code is shown with its length, so a short copy is visible", async () => {
  await issueACode();
  expect(screen.getByTestId("issued-enrolment-code-length").textContent)
    .toContain("32 characters");
});

test("Copy puts the exact code on the clipboard", async () => {
  await issueACode();
  const button = screen.getByTestId("issued-enrolment-code-copy");
  await act(async () => { fireEvent.click(button); });
  expect(navigator.clipboard.writeText).toHaveBeenCalledWith(CODE);
  await waitFor(() => expect(button.textContent).toMatch(/Copied/));
});

test("a browser with no clipboard API still copies", async () => {
  // Plain HTTP outside localhost - which is how a Store reaches this HQ.
  Object.assign(navigator, { clipboard: undefined });
  document.execCommand = jest.fn(() => true);
  await issueACode();
  const button = screen.getByTestId("issued-enrolment-code-copy");
  await act(async () => { fireEvent.click(button); });
  expect(document.execCommand).toHaveBeenCalledWith("copy");
});
