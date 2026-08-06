/**
 * What Broadcast History is allowed to say about a recording.
 *
 * A recording has five states and only one of them is a Play button. The
 * others must explain themselves rather than offering a control that would do
 * nothing, and PARTIAL in particular must stay visibly different from
 * AVAILABLE - "some of it is there" and "all of it is there" are different
 * promises about the same file.
 */
import React from "react";
import { render, screen, cleanup, waitFor, fireEvent } from "@testing-library/react";

import RecordingPlayer from "./RecordingPlayer";
import api from "@/lib/api";

jest.mock("@/lib/api", () => ({
  __esModule: true,
  default: { get: jest.fn() },
}));

function recording(overrides = {}) {
  return {
    status: "available",
    container: "webm",
    codec: "opus",
    byte_size: 262144,
    duration_seconds: 64,
    chunks_written: 256,
    chunks_dropped: 0,
    started_at: "2026-08-06T10:00:00+00:00",
    finalized_at: "2026-08-06T10:01:04+00:00",
    error: null,
    ...overrides,
  };
}

beforeEach(() => {
  api.get.mockReset();
  // jsdom implements neither, and both are part of how the audio reaches the
  // element without ever being a public URL.
  //
  // Re-created per test rather than once: CRA sets resetMocks, so a jest.fn()
  // installed in beforeAll is reset to returning undefined after the first
  // test - and a component that receives undefined for its blob URL renders
  // no player at all, which looks exactly like a bug in the component.
  global.URL.createObjectURL = jest.fn(() => "blob:recording");
  global.URL.revokeObjectURL = jest.fn();
});
afterEach(cleanup);

// ===========================================================================
// The five states
// ===========================================================================
test("a broadcast with no recording says so plainly", () => {
  render(<RecordingPlayer sessionId={12} recording={null} />);
  expect(screen.getByTestId("recording-none-12").textContent).toBe("No recording");
  expect(screen.queryByTestId("recording-play-12")).toBeNull();
});

test("an available recording offers playback with its duration and size", () => {
  render(<RecordingPlayer sessionId={12} recording={recording()} />);
  expect(screen.getByTestId("recording-play-12").textContent)
    .toMatch(/Play Recording/);
  expect(screen.getByTestId("recording-meta-12").textContent).toBe("1:04 · 256 KB");
});

test("a partial recording is playable AND visibly partial", () => {
  // A recording with a gap is still the best evidence of what went out, so it
  // must be playable - but it must never be presented as complete.
  render(<RecordingPlayer sessionId={12}
                          recording={recording({ status: "partial",
                                                 chunks_dropped: 12 })} />);
  expect(screen.getByTestId("recording-partial-12").textContent).toBe("Partial");
  expect(screen.getByTestId("recording-play-12")).toBeTruthy();
});

test("a failed recording explains itself instead of offering Play", () => {
  render(<RecordingPlayer sessionId={12}
                          recording={recording({ status: "failed",
                                                 error: "no space left on device" })} />);
  expect(screen.getByTestId("recording-problem-12").textContent)
    .toBe("Recording failed");
  expect(screen.queryByTestId("recording-play-12")).toBeNull();
});

test("a missing file is distinguished from a failed one", () => {
  // Different problems, different remedies: one is a disk that filled, the
  // other is a file that is no longer where the record says it is.
  render(<RecordingPlayer sessionId={12}
                          recording={recording({ status: "missing" })} />);
  expect(screen.getByTestId("recording-problem-12").textContent)
    .toBe("Recording missing");
  expect(screen.queryByTestId("recording-play-12")).toBeNull();
});

test("a recording still in progress is not offered for playback", () => {
  render(<RecordingPlayer sessionId={12}
                          recording={recording({ status: "recording",
                                                 duration_seconds: null })} />);
  expect(screen.getByTestId("recording-inprogress-12").textContent)
    .toBe("Recording…");
  expect(screen.queryByTestId("recording-play-12")).toBeNull();
});

// ===========================================================================
// Playback goes through the authenticated API
// ===========================================================================
test("playing fetches through the API client, not a bare URL", async () => {
  // The recordings folder is not a public mount. The token has to travel with
  // the request, which is why this goes through the API client at all.
  api.get.mockResolvedValue({ data: new Blob(["audio"]) });
  render(<RecordingPlayer sessionId={12} recording={recording()} />);

  fireEvent.click(screen.getByTestId("recording-play-12"));
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(
    "/broadcast/sessions/12/recording/audio", { responseType: "blob" }));

  const player = await screen.findByTestId("recording-audio-12");
  expect(player.getAttribute("src")).toBe("blob:recording");
  expect(player.hasAttribute("controls")).toBe(true);
});

test("an unauthorized recording is reported, not silently blank", async () => {
  api.get.mockRejectedValue({ response: { status: 403 } });
  render(<RecordingPlayer sessionId={12} recording={recording()} />);

  fireEvent.click(screen.getByTestId("recording-play-12"));
  expect((await screen.findByTestId("recording-error-12")).textContent)
    .toBe("You do not have access to this recording.");
  expect(screen.queryByTestId("recording-audio-12")).toBeNull();
});

test("a recording that vanished between listing and playing is reported", async () => {
  api.get.mockRejectedValue({ response: { status: 404 } });
  render(<RecordingPlayer sessionId={12} recording={recording()} />);

  fireEvent.click(screen.getByTestId("recording-play-12"));
  expect((await screen.findByTestId("recording-error-12")).textContent)
    .toBe("This recording could not be loaded.");
});

test("no filesystem path is ever rendered", () => {
  render(<RecordingPlayer sessionId={12} recording={recording()} />);
  const body = screen.getByTestId("recording-12").textContent;
  expect(body).not.toMatch(/[A-Za-z]:\\|\/data\/|\.part|broadcast-0000/);
});

test("nothing here claims the announcement was heard", () => {
  render(<RecordingPlayer sessionId={12} recording={recording()} />);
  expect(screen.getByTestId("recording-12").textContent.toLowerCase())
    .not.toMatch(/verified|audible|confirmed/);
});
