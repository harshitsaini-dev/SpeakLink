/**
 * HQ microphone gain and mute.
 *
 * The defect this guards against is subtle and serious: an operator mutes,
 * sees the meter still moving, and keeps talking believing the shops cannot
 * hear them - or the reverse. So these tests assert on the GAIN NODE and on
 * what MediaRecorder was handed, not on what the UI drew.
 *
 * The Web Audio API does not exist in jsdom, so the nodes are stubs that
 * record their connections. That is enough to prove the graph's shape and the
 * gain value; it cannot prove a browser resamples correctly, and nothing here
 * claims it does.
 */
import { HQBroadcaster } from "./HQBroadcaster";

function makeNode(kind) {
  return {
    kind,
    connected: [],
    connect(target) { this.connected.push(target); return target; },
    disconnect: jest.fn(),
  };
}

let contexts;

function installWebAudioStubs() {
  contexts = [];
  const track = { stop: jest.fn() };

  global.MediaRecorder = jest.fn(function MediaRecorderStub(stream, options) {
    this.stream = stream;
    this.options = options;
    this.state = "inactive";
    this.start = jest.fn(function start() { this.state = "recording"; });
    this.stop = jest.fn(function stop() { this.state = "inactive"; });
  });
  global.MediaRecorder.isTypeSupported = () => true;

  global.AudioContext = jest.fn(function AudioContextStub() {
    const gain = makeNode("gain");
    gain.gain = { value: 1, setTargetAtTime: jest.fn(function set(v) { gain.gain.value = v; }) };
    const destination = makeNode("destination");
    destination.stream = { id: "destination-stream", getTracks: () => [track] };
    const context = {
      currentTime: 0,
      gain,
      destination,
      analysers: [],
      source: null,
      closed: false,
      createMediaStreamSource: jest.fn(function createSource() {
        context.source = makeNode("source");
        return context.source;
      }),
      createGain: jest.fn(() => gain),
      createMediaStreamDestination: jest.fn(() => destination),
      createAnalyser: jest.fn(() => {
        const analyser = makeNode("analyser");
        analyser.fftSize = 0;
        analyser.frequencyBinCount = 8;
        analyser.getByteTimeDomainData = (buffer) => buffer.fill(128);
        context.analysers.push(analyser);
        return analyser;
      }),
      close: jest.fn(async () => { context.closed = true; }),
    };
    contexts.push(context);
    return context;
  });

  global.navigator.mediaDevices = {
    getUserMedia: jest.fn(async () => ({ getTracks: () => [track] })),
  };
  global.requestAnimationFrame = () => 1;   // one tick, no loop
  global.cancelAnimationFrame = jest.fn();

  global.WebSocket = jest.fn(function WebSocketStub() {
    this.readyState = 1;
    this.send = jest.fn();
    this.close = jest.fn();
    setTimeout(() => this.onopen && this.onopen(), 0);
  });

  return { track };
}

async function startBroadcaster(overrides = {}) {
  const bc = new HQBroadcaster({ wsUrl: "ws://test/ws", ...overrides });
  await bc.start();
  return bc;
}

beforeEach(() => { installWebAudioStubs(); });

// ===========================================================================
// Defaults and mapping
// ===========================================================================
test("the default is full volume, unmuted", async () => {
  const bc = await startBroadcaster();
  expect(bc.volumePercent).toBe(100);
  expect(bc.muted).toBe(false);
  expect(contexts[0].gain.gain.value).toBe(1);
});

test.each([[0, 0], [50, 0.5], [70, 0.7], [100, 1]])(
  "volume %i%% maps to gain %f", async (percent, expected) => {
    const bc = await startBroadcaster();
    bc.setVolumePercent(percent);
    expect(contexts[0].gain.gain.value).toBeCloseTo(expected, 5);
  });

test("volume is never boosted above unity", async () => {
  const bc = await startBroadcaster();
  // Nothing in the UI offers this; the guard is in the class so a future
  // caller cannot introduce clipping by passing a bigger number.
  bc.setVolumePercent(400);
  expect(bc.volumePercent).toBe(100);
  expect(contexts[0].gain.gain.value).toBeLessThanOrEqual(1);
});

test("a nonsense volume is ignored rather than silencing the broadcast", async () => {
  const bc = await startBroadcaster();
  bc.setVolumePercent(60);
  bc.setVolumePercent(Number.NaN);
  expect(bc.volumePercent).toBe(60);
});

// ===========================================================================
// Mute
// ===========================================================================
test("mute sets the effective gain to zero", async () => {
  const bc = await startBroadcaster();
  bc.setVolumePercent(70);
  bc.setMuted(true);
  expect(contexts[0].gain.gain.value).toBe(0);
  expect(bc.effectivelySilent).toBe(true);
});

test("unmute restores the previous volume, not 100%", async () => {
  const bc = await startBroadcaster();
  bc.setVolumePercent(70);
  bc.setMuted(true);
  bc.setMuted(false);
  expect(bc.volumePercent).toBe(70);
  expect(contexts[0].gain.gain.value).toBeCloseTo(0.7, 5);
});

test("mute does not stop the recorder, the microphone or the socket", async () => {
  // Muting by stopping the microphone would end the broadcast and release the
  // Store leases - an operator covering a cough would take the estate off air.
  const bc = await startBroadcaster();
  bc.setMuted(true);
  expect(bc.recorder.state).toBe("recording");
  expect(bc.recorder.stop).not.toHaveBeenCalled();
  expect(bc.stream).not.toBeNull();
  expect(bc.ws.close).not.toHaveBeenCalled();
});

// ===========================================================================
// The graph
// ===========================================================================
test("MediaRecorder is fed the post-gain destination, not the raw microphone", async () => {
  const bc = await startBroadcaster();
  // The whole feature rests on this: recording the raw stream would make the
  // gain node decorative.
  expect(bc.recorder.stream).toBe(contexts[0].destination.stream);
  expect(bc.recorder.stream).not.toBe(bc.stream);
});

test("the transport settings are unchanged by adding gain", async () => {
  const bc = await startBroadcaster();
  expect(bc.recorder.options.audioBitsPerSecond).toBe(32000);
  expect(bc.recorder.options.mimeType).toContain("webm");
  expect(bc.recorder.start).toHaveBeenCalledWith(250);
});

test("the source feeds both the input meter and the gain node", async () => {
  const bc = await startBroadcaster();
  const context = contexts[0];
  expect(context.source.connected).toContain(context.analysers[0]);
  expect(context.source.connected).toContain(context.gain);
  expect(context.gain.connected).toContain(context.destination);
  expect(bc.gainNode).toBe(context.gain);
});

// ===========================================================================
// Meter honesty
// ===========================================================================
test("the meter reports a sent level measured after the gain node", async () => {
  const readings = [];
  const bc = await startBroadcaster({
    onMeter: (level, detail) => readings.push({ level, detail }),
  });
  expect(readings.length).toBeGreaterThan(0);
  const last = readings[readings.length - 1];
  // Both halves are reported, so the UI can show a live microphone that is
  // nevertheless sending nothing.
  expect(last.detail).toHaveProperty("input");
  expect(last.detail).toHaveProperty("sent");
  expect(last.detail.muted).toBe(false);
  expect(bc.sentAnalyser).toBe(contexts[0].analysers[1]);
});

test("the meter reports the muted state so the UI cannot imply audio is sent", async () => {
  const readings = [];
  const bc = await startBroadcaster({
    onMeter: (level, detail) => readings.push(detail),
  });
  readings.length = 0;
  bc.setMuted(true);
  bc._runMeter();
  expect(readings[readings.length - 1].muted).toBe(true);
});

// ===========================================================================
// Cleanup
// ===========================================================================
test("stop closes the context, stops the tracks and disconnects the graph", async () => {
  const bc = await startBroadcaster();
  const context = contexts[0];
  const source = context.source;
  const gain = context.gain;
  await bc.stop();

  expect(context.close).toHaveBeenCalled();
  expect(source.disconnect).toHaveBeenCalled();
  expect(gain.disconnect).toHaveBeenCalled();
  // Every reference dropped, so a restart cannot layer a second graph.
  expect(bc.audioCtx).toBeNull();
  expect(bc.gainNode).toBeNull();
  expect(bc.destination).toBeNull();
  expect(bc.stream).toBeNull();
});

test("a level set before start is carried into the graph", async () => {
  // The Console applies the operator's current setting to a NEW broadcaster,
  // so starting a second broadcast does not silently jump back to 100%.
  const bc = new HQBroadcaster({ wsUrl: "ws://test/ws" });
  bc.setVolumePercent(40);
  bc.setMuted(false);
  await bc.start();
  expect(contexts[0].gain.gain.value).toBeCloseTo(0.4, 5);
});
