/**
 * Emergency Stop, now that it lives in the sidebar instead of the Console.
 *
 * These assertions moved here from BroadcastConsole.multibroadcast.test.js
 * unchanged in substance: the control changed address, not meaning. What they
 * are protecting is that Emergency Stop keeps its own permission, its own
 * confirmation naming the blast radius, and - the one that matters most - that
 * a partial failure is never rendered as a success. An operator who reads
 * "stopped" about a broadcast that is still live will walk away from it.
 */
import React from "react";
import { render, screen, act, cleanup, fireEvent } from "@testing-library/react";
import EmergencyStopControl from "./EmergencyStopControl";

let mockPermissions;
let mockBroadcast;

jest.mock("@/contexts/BroadcastContext", () => ({
  useBroadcast: () => mockBroadcast,
}));
jest.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ can: (code) => mockPermissions.has(code) }),
}));

function renderControl(permissions = ["broadcast.emergency_stop"]) {
  mockPermissions = new Set(permissions);
  render(<EmergencyStopControl />);
}

beforeEach(() => {
  mockBroadcast = {
    emergencyStop: jest.fn(async () => ({ ok: true, session_ids: [1, 2] })),
  };
});

afterEach(cleanup);


test("an account without the permission gets no Emergency Stop button", () => {
  renderControl(["broadcast.start", "broadcast.stop"]);
  expect(screen.queryByTestId("emergency-stop-btn")).toBeNull();
});

test("an account with the permission gets the button", () => {
  renderControl();
  expect(screen.getByTestId("emergency-stop-btn")).toBeTruthy();
});

test("Emergency Stop asks its own confirmation naming ALL broadcasts", async () => {
  renderControl();

  await act(async () => {
    fireEvent.click(screen.getByTestId("emergency-stop-btn"));
  });

  const words = screen.getByTestId("emergency-confirm-modal").textContent.toLowerCase();
  expect(words).toContain("all active");
  expect(words).toContain("other operators");
});

test("confirming reports how many were stopped", async () => {
  renderControl();

  await act(async () => {
    fireEvent.click(screen.getByTestId("emergency-stop-btn"));
  });
  await act(async () => {
    fireEvent.click(screen.getByTestId("emergency-confirm-btn"));
  });

  expect(mockBroadcast.emergencyStop).toHaveBeenCalled();
  expect(screen.getByTestId("emergency-result").textContent).toContain("2");
});

test("no active broadcasts says exactly that, rather than claiming a stop", async () => {
  mockBroadcast.emergencyStop = jest.fn(async () => ({ ok: true, session_ids: [] }));
  renderControl();

  await act(async () => {
    fireEvent.click(screen.getByTestId("emergency-stop-btn"));
  });
  await act(async () => {
    fireEvent.click(screen.getByTestId("emergency-confirm-btn"));
  });

  expect(screen.getByTestId("emergency-result").textContent)
    .toMatch(/no active broadcasts/i);
});

test("a partial failure renders an error, never a success message", async () => {
  const partial = new Error(
    "SOME BROADCASTS ARE STILL LIVE. Not every broadcast could be stopped.");
  partial.emergencyIncomplete = true;
  partial.failedSessionIds = [2];
  mockBroadcast.emergencyStop = jest.fn(async () => { throw partial; });
  renderControl();

  await act(async () => {
    fireEvent.click(screen.getByTestId("emergency-stop-btn"));
  });
  await act(async () => {
    fireEvent.click(screen.getByTestId("emergency-confirm-btn"));
  });

  const result = screen.getByTestId("emergency-result");
  expect(result.textContent).toContain("STILL LIVE");
  // The success sentence is "Emergency Stop: N broadcasts stopped." - the
  // refusal must never be rendered in that shape, whatever words it contains.
  expect(result.textContent).not.toMatch(/Emergency Stop: \d+ broadcast/);
});

test("cancelling the confirmation stops nothing", async () => {
  renderControl();

  await act(async () => {
    fireEvent.click(screen.getByTestId("emergency-stop-btn"));
  });
  await act(async () => {
    fireEvent.click(screen.getByTestId("emergency-cancel-btn"));
  });

  expect(mockBroadcast.emergencyStop).not.toHaveBeenCalled();
  expect(screen.queryByTestId("emergency-confirm-modal")).toBeNull();
});

test("the outcome stays until it is dismissed", async () => {
  // An outcome that vanished on its own is one an operator can miss.
  renderControl();
  await act(async () => {
    fireEvent.click(screen.getByTestId("emergency-stop-btn"));
  });
  await act(async () => {
    fireEvent.click(screen.getByTestId("emergency-confirm-btn"));
  });
  expect(screen.getByTestId("emergency-result")).toBeTruthy();

  await act(async () => {
    fireEvent.click(screen.getByTestId("emergency-result-dismiss"));
  });
  expect(screen.queryByTestId("emergency-result")).toBeNull();
});
