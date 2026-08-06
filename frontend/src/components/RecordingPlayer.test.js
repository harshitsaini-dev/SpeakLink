/**
 * The Broadcast History recording bar, and the row actions that feed it.
 *
 * TWO THINGS THESE TESTS EXIST TO PROTECT
 *
 * **The volume slider is local.** In every other part of EchoCast a control
 * labelled "volume" changes a shop's speakers. This one changes an
 * HTMLAudioElement in the operator's own browser and must never generate a
 * Store command. That is asserted explicitly rather than assumed.
 *
 * **Playback state must be true.** "Playing" comes from the audio element's
 * own events, never from the fact that a button was pressed - so a file that
 * fails to decode does not sit there claiming to play.
 */
import React from "react";
import { render, screen, cleanup, waitFor, fireEvent, act } from "@testing-library/react";

import RecordingPlayer from "./RecordingPlayer";
import RecordingActions from "./RecordingActions";
import { api } from "@/lib/api";

// Mocked as a NAMED export because that is what the module really has.
jest.mock("@/lib/api", () => ({
  __esModule: true,
  api: { get: jest.fn(), post: jest.fn(), delete: jest.fn() },
}));

function recording(overrides = {}) {
  return {
    status: "available",
    container: "webm",
    codec: "opus",
    byte_size: 29696,
    duration_seconds: null,
    chunks_written: 256,
    chunks_dropped: 0,
    started_at: "2026-08-06T10:00:00+00:00",
    finalized_at: "2026-08-06T10:01:04+00:00",
    error: null,
    ...overrides,
  };
}

function session(id = 12, overrides = {}) {
  return {
    id,
    campaign_name: id === 12 ? "Morning announcement" : "Evening reminder",
    started_at: "2026-08-06T10:00:00+00:00",
    recording: recording(),
    ...overrides,
  };
}

/** jsdom has no media stack, so the element's own behaviour is supplied. */
function stubAudio() {
  const element = HTMLMediaElement.prototype;
  Object.defineProperty(element, "paused", {
    configurable: true, writable: true, value: true,
  });
  element.play = jest.fn(function play() {
    this.paused = false;
    fireEvent.play(this);
    return Promise.resolve();
  });
  element.pause = jest.fn(function pause() {
    this.paused = true;
    fireEvent.pause(this);
  });
}

beforeEach(() => {
  api.get.mockReset();
  api.post.mockReset();
  api.delete.mockReset();
  // Re-created per test: CRA sets resetMocks, so a jest.fn() installed once
  // returns undefined afterwards - and a component handed undefined for its
  // blob URL renders no player, which looks exactly like a component bug.
  global.URL.createObjectURL = jest.fn(() => "blob:recording");
  global.URL.revokeObjectURL = jest.fn();
  stubAudio();
});
afterEach(cleanup);

/** Render the bar on a recording and wait for its audio to arrive. */
async function showBar(active = session(12), playToken = 0) {
  api.get.mockResolvedValue({ data: new Blob(["audio"]) });
  const view = render(
    <RecordingPlayer session={active} playToken={playToken} onClose={() => {}} />);
  await waitFor(() =>
    expect(screen.getByTestId("recording-toggle").disabled).toBe(false));
  return view;
}

// ===========================================================================
// The History row
// ===========================================================================
test("a recorded row offers Play and Download as EchoCast actions", () => {
  render(<RecordingActions sessionId={12} recording={recording()} onPlay={() => {}} />);
  const play = screen.getByTestId("recording-play-12");
  const download = screen.getByTestId("recording-download-12");
  // The same shape Rights and Scope use in User Management.
  for (const button of [play, download]) {
    expect(button.tagName).toBe("BUTTON");
    expect(button.className).toContain("inline-flex");
    expect(button.className).toContain("rounded");
    expect(button.className).toContain("border");
  }
  expect(play.textContent).toMatch(/Play/);
  expect(download.textContent).toMatch(/Download/);
});

test("the row shows the recording size", () => {
  render(<RecordingActions sessionId={12} recording={recording()} onPlay={() => {}} />);
  expect(screen.getByTestId("recording-meta-12").textContent).toBe("29 KB");
});

test("the row never embeds an audio element", () => {
  const { container } = render(
    <RecordingActions sessionId={12} recording={recording()} onPlay={() => {}} />);
  expect(container.querySelector("audio")).toBeNull();
});

test("the row's Play only asks the page to make this recording active", () => {
  const onPlay = jest.fn();
  render(<RecordingActions sessionId={12} recording={recording()} onPlay={onPlay} />);
  fireEvent.click(screen.getByTestId("recording-play-12"));
  expect(onPlay).toHaveBeenCalledWith(12);
  // The row itself fetches nothing: the bar owns loading.
  expect(api.get).not.toHaveBeenCalled();
});

test("the row's Download uses the authenticated download route", async () => {
  api.get.mockResolvedValue({ data: new Blob(["audio"]) });
  render(<RecordingActions sessionId={12} recording={recording()} onPlay={() => {}} />);
  fireEvent.click(screen.getByTestId("recording-download-12"));
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(
    "/broadcast/sessions/12/recording/download", { responseType: "blob" }));
});

test("the downloaded filename carries only the session id", async () => {
  api.get.mockResolvedValue({ data: new Blob(["audio"]) });
  const names = [];
  const realCreate = document.createElement.bind(document);
  jest.spyOn(document, "createElement").mockImplementation((tag) => {
    const node = realCreate(tag);
    if (tag === "a") node.click = () => names.push(node.download);
    return node;
  });

  render(<RecordingActions sessionId={12} recording={recording()} onPlay={() => {}} />);
  fireEvent.click(screen.getByTestId("recording-download-12"));
  await waitFor(() => expect(names).toEqual(["broadcast-000012.webm"]));
  document.createElement.mockRestore();
});

// ===========================================================================
// The bar exists only when a recording is active
// ===========================================================================
test("no player is rendered until a recording is chosen", () => {
  const { container } = render(
    <RecordingPlayer session={null} onClose={() => {}} />);
  expect(screen.queryByTestId("recording-player-bar")).toBeNull();
  expect(container.querySelector("audio")).toBeNull();
});

test("choosing a recording shows a fixed bar at the bottom", async () => {
  await showBar();
  const bar = screen.getByTestId("recording-player-bar");
  expect(bar.className).toContain("fixed");
  expect(bar.className).toContain("bottom-0");
  expect(bar.className).toContain("right-0");
  // Starts where the sidebar ends, so it never covers the navigation.
  expect(bar.className).toContain("md:left-64");
});

test("the bar is a labelled region, not a modal dialog", async () => {
  // It is a toolbar the operator works alongside: no backdrop, no focus trap,
  // and History's filters and pagination stay usable behind it.
  await showBar();
  const bar = screen.getByTestId("recording-player-bar");
  expect(bar.tagName).toBe("SECTION");
  expect(bar.getAttribute("role")).toBeNull();
  expect(bar.getAttribute("aria-label")).toBe("Broadcast recording player");
});

test("the bar carries no anchored-popover positioning", async () => {
  await showBar();
  const bar = screen.getByTestId("recording-player-bar");
  // Nothing measured from a button: no inline top/left placement survives.
  expect(bar.style.top).toBe("");
  expect(bar.style.left).toBe("");
});

test("the bar names the broadcast being listened to", async () => {
  await showBar();
  expect(screen.getByTestId("recording-campaign").textContent)
    .toBe("Morning announcement");
  expect(screen.getByTestId("recording-session").textContent)
    .toMatch(/Broadcast #12/);
});

test("the audio is fetched through the authenticated API", async () => {
  await showBar();
  expect(api.get).toHaveBeenCalledWith(
    "/broadcast/sessions/12/recording/audio", { responseType: "blob" });
});

test("the browser's native controls are never rendered", async () => {
  const { container } = await showBar();
  const audio = container.querySelector("audio");
  expect(audio).toBeTruthy();
  expect(audio.hasAttribute("controls")).toBe(false);
  expect(audio.className).toContain("hidden");
});

// ===========================================================================
// Playback
// ===========================================================================
test("the play control starts the audio element", async () => {
  const { container } = await showBar();
  const audio = container.querySelector("audio");

  await act(async () => { fireEvent.click(screen.getByTestId("recording-toggle")); });
  expect(audio.play).toHaveBeenCalled();
  expect(screen.getByTestId("recording-state").textContent).toBe("Playing");
});

test("pressing it again pauses", async () => {
  const { container } = await showBar();
  const audio = container.querySelector("audio");

  await act(async () => { fireEvent.click(screen.getByTestId("recording-toggle")); });
  await act(async () => { fireEvent.click(screen.getByTestId("recording-toggle")); });
  expect(audio.pause).toHaveBeenCalled();
  expect(screen.getByTestId("recording-state").textContent).toBe("Paused");
});

test("the elapsed time follows the audio element", async () => {
  const { container } = await showBar();
  const audio = container.querySelector("audio");

  Object.defineProperty(audio, "currentTime", { configurable: true, value: 62 });
  fireEvent.timeUpdate(audio);
  expect(screen.getByTestId("recording-position").textContent).toBe("1:02");
});

test("a duration the file does not carry is shown as unavailable", async () => {
  // MediaRecorder writes a streaming WebM header with no duration, so the
  // element may report Infinity for ever. A guess would be a lie, and 0:00
  // would read as a real position.
  const { container } = await showBar();
  const audio = container.querySelector("audio");

  Object.defineProperty(audio, "duration", { configurable: true, value: Infinity });
  fireEvent.durationChange(audio);
  expect(screen.getByTestId("recording-duration").textContent).toBe("—:—");
  expect(screen.getByTestId("recording-seek").disabled).toBe(true);
});

test("seeking sets currentTime once a real duration is known", async () => {
  const { container } = await showBar();
  const audio = container.querySelector("audio");

  Object.defineProperty(audio, "duration", { configurable: true, value: 90 });
  let assigned = 0;
  Object.defineProperty(audio, "currentTime", {
    configurable: true, get: () => assigned, set: (value) => { assigned = value; },
  });
  fireEvent.loadedMetadata(audio);
  expect(screen.getByTestId("recording-duration").textContent).toBe("1:30");

  fireEvent.change(screen.getByTestId("recording-seek"), { target: { value: "45" } });
  expect(assigned).toBe(45);
});

test("forward and back move by ten seconds", async () => {
  const { container } = await showBar();
  const audio = container.querySelector("audio");

  Object.defineProperty(audio, "duration", { configurable: true, value: 90 });
  let assigned = 30;
  Object.defineProperty(audio, "currentTime", {
    configurable: true, get: () => assigned, set: (value) => { assigned = value; },
  });
  fireEvent.loadedMetadata(audio);

  fireEvent.click(screen.getByTestId("recording-forward"));
  expect(assigned).toBe(40);
  fireEvent.click(screen.getByTestId("recording-back"));
  expect(assigned).toBe(30);
});

test("finishing is reported as finished, not as still playing", async () => {
  const { container } = await showBar();
  const audio = container.querySelector("audio");

  await act(async () => { fireEvent.click(screen.getByTestId("recording-toggle")); });
  fireEvent.ended(audio);
  expect(screen.getByTestId("recording-state").textContent).toBe("Finished");
});

test("a file that cannot be decoded says so rather than claiming to play", async () => {
  const { container } = await showBar();
  fireEvent.error(container.querySelector("audio"));
  expect(screen.getByTestId("recording-state").textContent).toBe("Playback failed");
});

// ===========================================================================
// Volume is LOCAL
// ===========================================================================
test("the volume slider changes only the audio element", async () => {
  const { container } = await showBar();
  const audio = container.querySelector("audio");

  fireEvent.change(screen.getByTestId("recording-volume"), { target: { value: "0.4" } });
  expect(audio.volume).toBeCloseTo(0.4);
});

test("mute toggles only the audio element and keeps the chosen level", async () => {
  const { container } = await showBar();
  const audio = container.querySelector("audio");

  fireEvent.change(screen.getByTestId("recording-volume"), { target: { value: "0.6" } });
  fireEvent.click(screen.getByTestId("recording-mute"));
  expect(audio.muted).toBe(true);
  expect(audio.volume).toBeCloseTo(0.6, 5);   // the level survives the mute

  fireEvent.click(screen.getByTestId("recording-mute"));
  expect(audio.muted).toBe(false);
});

test("nothing in the player ever calls a Store audio control", async () => {
  // The whole point. A volume slider in EchoCast usually moves a shop's
  // speakers; this one must not reach one.
  await showBar();

  fireEvent.change(screen.getByTestId("recording-volume"), { target: { value: "0.2" } });
  fireEvent.click(screen.getByTestId("recording-mute"));
  await act(async () => { fireEvent.click(screen.getByTestId("recording-toggle")); });

  expect(api.post).not.toHaveBeenCalled();
  expect(api.delete).not.toHaveBeenCalled();
  const requested = api.get.mock.calls.map(([path]) => path);
  expect(requested.every((path) => path.includes("/recording/"))).toBe(true);
  expect(requested.some((path) => path.includes("store-audio"))).toBe(false);
});

// ===========================================================================
// Download from the bar
// ===========================================================================
test("the bar's Download uses the authenticated download route", async () => {
  await showBar();
  api.get.mockClear();
  api.get.mockResolvedValue({ data: new Blob(["audio"]) });

  fireEvent.click(screen.getByTestId("recording-bar-download"));
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(
    "/broadcast/sessions/12/recording/download", { responseType: "blob" }));
});

test("no request ever carries a token or a filesystem path", async () => {
  await showBar();
  fireEvent.click(screen.getByTestId("recording-bar-download"));
  await waitFor(() => expect(api.get.mock.calls.length).toBeGreaterThan(1));
  for (const [path] of api.get.mock.calls) {
    expect(path).not.toMatch(/token|[A-Za-z]:\\|\/data\/|\.part/);
  }
});

// ===========================================================================
// Switching, closing and cleanup
// ===========================================================================
test("switching recordings pauses the first and releases its audio", async () => {
  api.get.mockResolvedValue({ data: new Blob(["audio"]) });
  const { rerender, container } = render(
    <RecordingPlayer session={session(12)} onClose={() => {}} />);
  await waitFor(() =>
    expect(screen.getByTestId("recording-toggle").disabled).toBe(false));
  const audio = container.querySelector("audio");
  await act(async () => { fireEvent.click(screen.getByTestId("recording-toggle")); });

  await act(async () => {
    rerender(<RecordingPlayer session={session(13)} onClose={() => {}} />);
  });

  expect(audio.pause).toHaveBeenCalled();
  expect(global.URL.revokeObjectURL).toHaveBeenCalledWith("blob:recording");
  expect(screen.getByTestId("recording-session").textContent).toMatch(/Broadcast #13/);
});

test("there is only ever one audio element", async () => {
  api.get.mockResolvedValue({ data: new Blob(["audio"]) });
  const { rerender, container } = render(
    <RecordingPlayer session={session(12)} onClose={() => {}} />);
  await waitFor(() => expect(container.querySelectorAll("audio").length).toBe(1));

  await act(async () => {
    rerender(<RecordingPlayer session={session(13)} onClose={() => {}} />);
  });
  expect(container.querySelectorAll("audio").length).toBe(1);
});

test("Escape closes the player", async () => {
  const onClose = jest.fn();
  api.get.mockResolvedValue({ data: new Blob(["audio"]) });
  render(<RecordingPlayer session={session(12)} onClose={onClose} />);
  await screen.findByTestId("recording-player-bar");

  fireEvent.keyDown(document, { key: "Escape" });
  expect(onClose).toHaveBeenCalled();
});

test("the close button closes the player", async () => {
  const onClose = jest.fn();
  api.get.mockResolvedValue({ data: new Blob(["audio"]) });
  render(<RecordingPlayer session={session(12)} onClose={onClose} />);
  await screen.findByTestId("recording-player-bar");

  fireEvent.click(screen.getByTestId("recording-close"));
  expect(onClose).toHaveBeenCalled();
});

test("clicking elsewhere does NOT close the player", async () => {
  // Unlike the popover this replaced. An operator scrolling History or
  // ticking a checkbox must not silently lose what they are listening to.
  const onClose = jest.fn();
  api.get.mockResolvedValue({ data: new Blob(["audio"]) });
  render(<RecordingPlayer session={session(12)} onClose={onClose} />);
  await screen.findByTestId("recording-player-bar");

  fireEvent.mouseDown(document.body);
  fireEvent.click(document.body);
  expect(onClose).not.toHaveBeenCalled();
  expect(screen.getByTestId("recording-player-bar")).toBeTruthy();
});

test("unmounting pauses playback and releases the blob URL", async () => {
  const { unmount, container } = await showBar();
  const audio = container.querySelector("audio");
  await act(async () => { fireEvent.click(screen.getByTestId("recording-toggle")); });

  unmount();
  expect(audio.pause).toHaveBeenCalled();
  expect(global.URL.revokeObjectURL).toHaveBeenCalledWith("blob:recording");
});

test("clearing the active recording tears the player down", async () => {
  // What the History page does when the row being played is deleted.
  api.get.mockResolvedValue({ data: new Blob(["audio"]) });
  const { rerender, container } = render(
    <RecordingPlayer session={session(12)} onClose={() => {}} />);
  await waitFor(() =>
    expect(screen.getByTestId("recording-toggle").disabled).toBe(false));
  const audio = container.querySelector("audio");

  await act(async () => {
    rerender(<RecordingPlayer session={null} onClose={() => {}} />);
  });
  expect(screen.queryByTestId("recording-player-bar")).toBeNull();
  expect(audio.pause).toHaveBeenCalled();
  expect(global.URL.revokeObjectURL).toHaveBeenCalledWith("blob:recording");
});

// ===========================================================================
// The states that offer no player at all
// ===========================================================================
test("a failed recording has no Play", () => {
  render(<RecordingActions sessionId={12}
                           recording={recording({ status: "failed" })}
                           onPlay={() => {}} />);
  expect(screen.getByTestId("recording-problem-12").textContent).toBe("Recording failed");
  expect(screen.queryByTestId("recording-play-12")).toBeNull();
});

test("a missing recording has no Play", () => {
  render(<RecordingActions sessionId={12}
                           recording={recording({ status: "missing" })}
                           onPlay={() => {}} />);
  expect(screen.getByTestId("recording-problem-12").textContent).toBe("Recording missing");
  expect(screen.queryByTestId("recording-play-12")).toBeNull();
});

test("a recording still being written has no Play", () => {
  render(<RecordingActions sessionId={12}
                           recording={recording({ status: "recording" })}
                           onPlay={() => {}} />);
  expect(screen.getByTestId("recording-inprogress-12").textContent).toBe("Recording…");
  expect(screen.queryByTestId("recording-play-12")).toBeNull();
});

test("a broadcast with no recording says so", () => {
  render(<RecordingActions sessionId={12} recording={null} onPlay={() => {}} />);
  expect(screen.getByTestId("recording-none-12").textContent).toBe("No recording");
  expect(screen.queryByTestId("recording-play-12")).toBeNull();
});

test("a partial recording is playable and visibly partial", () => {
  render(<RecordingActions sessionId={12}
                           recording={recording({ status: "partial", chunks_dropped: 12 })}
                           onPlay={() => {}} />);
  expect(screen.getByTestId("recording-partial-12").textContent).toBe("Partial");
  expect(screen.getByTestId("recording-play-12")).toBeTruthy();
});

// ===========================================================================
// Failures and accessibility
// ===========================================================================
test("an unauthorized load is reported, not silently blank", async () => {
  api.get.mockRejectedValue({ response: { status: 403 } });
  render(<RecordingPlayer session={session(12)} onClose={() => {}} />);
  expect((await screen.findByTestId("recording-bar-error")).textContent)
    .toBe("You do not have access to this recording.");
});

test("every control is a real button with a name", async () => {
  await showBar();
  for (const id of ["recording-toggle", "recording-mute", "recording-close",
                    "recording-back", "recording-forward"]) {
    const control = screen.getByTestId(id);
    expect(control.tagName).toBe("BUTTON");
    expect(control.getAttribute("aria-label")).toBeTruthy();
  }
  expect(screen.getByTestId("recording-seek").getAttribute("aria-label")).toBe("Seek");
  expect(screen.getByTestId("recording-volume").getAttribute("aria-label"))
    .toBe("Playback volume");
});

test("keyboard activation works on the row's Play", () => {
  const onPlay = jest.fn();
  render(<RecordingActions sessionId={12} recording={recording()} onPlay={onPlay} />);
  const play = screen.getByTestId("recording-play-12");
  play.focus();
  expect(document.activeElement).toBe(play);
  // A real <button> fires click for Enter and Space, which is exactly why
  // these are buttons rather than styled divs.
  fireEvent.click(play);
  expect(onPlay).toHaveBeenCalled();
});


// ===========================================================================
// One click on History's Play means PLAY
// ===========================================================================
test("a play intent starts the audio without a second click", async () => {
  // The defect this exists to stop: the row button opened the bar and the
  // operator then had to press the footer's own Play.
  api.get.mockResolvedValue({ data: new Blob(["audio"]) });
  const { container } = render(
    <RecordingPlayer session={session(12)} playToken={1} onClose={() => {}} />);

  const audio = container.querySelector("audio");
  await waitFor(() => expect(audio.play).toHaveBeenCalled());
  await waitFor(() =>
    expect(screen.getByTestId("recording-state").textContent).toBe("Playing"));
});

test("selecting without a play intent does not start anything", async () => {
  const { container } = await showBar(session(12), 0);
  expect(container.querySelector("audio").play).not.toHaveBeenCalled();
});

test("autoplay waits for the audio, and claims nothing before it", async () => {
  // Nothing may say Playing until the element's own event fires.
  let resolveFetch;
  api.get.mockReturnValue(new Promise((resolve) => { resolveFetch = resolve; }));
  const { container } = render(
    <RecordingPlayer session={session(12)} playToken={1} onClose={() => {}} />);

  // The point: nothing is played, and nothing claims to be playing, until
  // there is actually audio to play.
  expect(container.querySelector("audio").play).not.toHaveBeenCalled();
  expect(screen.getByTestId("recording-state").textContent).not.toBe("Playing");

  await act(async () => { resolveFetch({ data: new Blob(["audio"]) }); });
  await waitFor(() =>
    expect(container.querySelector("audio").play).toHaveBeenCalled());
});

test("a browser that refuses to autoplay says so truthfully", async () => {
  api.get.mockResolvedValue({ data: new Blob(["audio"]) });
  HTMLMediaElement.prototype.play = jest.fn(() =>
    Promise.reject(new Error("not allowed")));

  render(<RecordingPlayer session={session(12)} playToken={1} onClose={() => {}} />);
  expect((await screen.findByTestId("recording-bar-error")).textContent)
    .toMatch(/could not start automatically/i);
});

test("choosing a different recording autoplays the new one from the start", async () => {
  api.get.mockResolvedValue({ data: new Blob(["audio"]) });
  const { rerender, container } = render(
    <RecordingPlayer session={session(12)} playToken={1} onClose={() => {}} />);
  const audio = container.querySelector("audio");
  await waitFor(() => expect(audio.play).toHaveBeenCalledTimes(1));

  await act(async () => {
    rerender(<RecordingPlayer session={session(13)} playToken={2}
                              onClose={() => {}} />);
  });
  await waitFor(() => expect(audio.play).toHaveBeenCalledTimes(2));
  expect(audio.pause).toHaveBeenCalled();
  expect(screen.getByTestId("recording-session").textContent)
    .toMatch(/Broadcast #13/);
});

test("asking again for a paused recording resumes it", async () => {
  api.get.mockResolvedValue({ data: new Blob(["audio"]) });
  const { rerender, container } = render(
    <RecordingPlayer session={session(12)} playToken={1} onClose={() => {}} />);
  const audio = container.querySelector("audio");
  await waitFor(() => expect(audio.play).toHaveBeenCalledTimes(1));

  await act(async () => { fireEvent.click(screen.getByTestId("recording-toggle")); });
  expect(audio.paused).toBe(true);

  await act(async () => {
    rerender(<RecordingPlayer session={session(12)} playToken={2}
                              onClose={() => {}} />);
  });
  await waitFor(() => expect(audio.play).toHaveBeenCalledTimes(2));
  // Resumed, not reloaded: the same source is still in place.
  expect(api.get).toHaveBeenCalledTimes(1);
});

test("a live broadcast starting pauses playback but keeps the selection", async () => {
  // A recording out of the HQ speakers can be picked up by the HQ microphone.
  api.get.mockResolvedValue({ data: new Blob(["audio"]) });
  const { rerender, container } = render(
    <RecordingPlayer session={session(12)} playToken={1} pauseToken={0}
                     onClose={() => {}} />);
  const audio = container.querySelector("audio");
  await waitFor(() => expect(audio.play).toHaveBeenCalled());

  await act(async () => {
    rerender(<RecordingPlayer session={session(12)} playToken={1} pauseToken={1}
                              onClose={() => {}} />);
  });
  expect(audio.pause).toHaveBeenCalled();
  // Still selected, so the operator can carry on afterwards.
  expect(screen.getByTestId("recording-player-bar")).toBeTruthy();
});
