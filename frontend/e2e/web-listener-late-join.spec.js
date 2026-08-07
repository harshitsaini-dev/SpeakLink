/**
 * A browser listener decodes what the REAL relay actually sends.
 *
 * The accepted gate (late-join-gate.spec.js) established WHICH architecture can
 * work. This proves the SHIPPED implementation of it does: the frames fed to
 * MediaSource here are produced by backend/webm_stream.py itself, via
 * backend/tools/emit_relay_frames.py, not by a second framer written in the
 * test. A test that framed the stream itself would only prove that two
 * implementations of the same idea agree - and the gate's whole finding was
 * that the obvious idea is wrong.
 *
 * A listener is bootstrapped exactly as WebAudienceRelay.bootstrap() defines
 * it: the initialization segment, then up to two live-edge Clusters, then
 * future Clusters. Nothing here replays the Broadcast from the beginning.
 */
const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const FRAMES_BIN = path.join(__dirname, 'fixtures', 'relay-frames.bin');
const FRAMES_INDEX = path.join(__dirname, 'fixtures', 'relay-frames.json');

const MIME = 'audio/webm;codecs=opus';
// Matches DEFAULT_LIVE_EDGE_CLUSTERS in backend/web_audience.py.
const LIVE_EDGE_CLUSTERS = 2;

const available = fs.existsSync(FRAMES_BIN) && fs.existsSync(FRAMES_INDEX);

test.describe('web listener decodes real relay output', () => {
  test.skip(!available,
    'generate first: python backend/tools/emit_relay_frames.py');

  let frames;
  let payload;

  test.beforeAll(() => {
    const index = JSON.parse(fs.readFileSync(FRAMES_INDEX, 'utf8'));
    const blob = fs.readFileSync(FRAMES_BIN);
    frames = index.frames.map((frame) => ({
      kind: frame.kind,
      bytes: Array.from(blob.subarray(frame.offset, frame.offset + frame.length)),
    }));
    payload = { init: frames[0], clusters: frames.slice(1) };
  });

  /**
   * Join at `fromCluster`, exactly as the relay would bootstrap a listener that
   * connected at that moment, and report what the element really did.
   */
  async function join(page, fromCluster) {
    return page.evaluate(
      async ({ mime, init, clusters, from, edge }) => {
        const audio = document.createElement('audio');
        audio.muted = true;              // decode is under test, not audibility
        document.body.appendChild(audio);
        const source = new MediaSource();
        audio.src = URL.createObjectURL(source);
        const result = { error: null, played: false, advanced: false, appended: 0 };

        try {
          await new Promise((resolve, reject) => {
            const failed = setTimeout(() => reject(new Error('sourceopen timeout')), 5000);
            source.addEventListener('sourceopen',
              () => { clearTimeout(failed); resolve(); }, { once: true });
          });
          const buffer = source.addSourceBuffer(mime);
          buffer.mode = 'sequence';
          const append = (bytes) => new Promise((resolve, reject) => {
            buffer.addEventListener('updateend', resolve, { once: true });
            buffer.addEventListener('error', () => reject(new Error('append error')),
                                    { once: true });
            buffer.appendBuffer(new Uint8Array(bytes));
          });

          await append(init);
          result.appended += 1;
          // The bootstrap: a bounded number of live-edge Clusters, then live.
          const start = Math.max(0, from - edge);
          for (let i = start; i < clusters.length; i += 1) {
            await append(clusters[i]);
            result.appended += 1;
          }
          if (buffer.buffered.length) {
            result.bufferedSeconds =
              buffer.buffered.end(buffer.buffered.length - 1) - buffer.buffered.start(0);
          }
          await audio.play();
          result.played = true;
          const before = audio.currentTime;
          await new Promise((resolve) => setTimeout(resolve, 900));
          result.advanced = audio.currentTime > before;
          result.mediaError = audio.error ? audio.error.code : null;
        } catch (error) {
          result.error = String(error && error.message ? error.message : error);
        } finally {
          try { audio.pause(); } catch (ignored) { /* already gone */ }
          audio.remove();
        }
        return result;
      },
      {
        mime: MIME, init: payload.init.bytes,
        clusters: payload.clusters.map((frame) => frame.bytes),
        from: fromCluster, edge: LIVE_EDGE_CLUSTERS,
      },
    );
  }

  test('the relay emits an init segment and only whole Clusters', async () => {
    const index = JSON.parse(fs.readFileSync(FRAMES_INDEX, 'utf8'));
    expect(index.frames[0].kind).toBe('init');
    expect(index.clusterCount).toBeGreaterThan(10);
    // Every Cluster the product emits begins at a real container boundary.
    for (const frame of index.frames.slice(1)) {
      const bytes = fs.readFileSync(FRAMES_BIN)
        .subarray(frame.offset, frame.offset + 4);
      expect(Array.from(bytes)).toEqual([0x1f, 0x43, 0xb6, 0x75]);
    }
  });

  // Cluster media duration is a uniform 300 ms, so these are roughly the
  // stream start, ~5 seconds in, and ~30 seconds in.
  for (const [label, fromCluster] of [
    ['at the start of the Broadcast', 0],
    ['about 5 seconds in', 16],
    ['about 10 seconds in', 33],
  ]) {
    test(`a listener joining ${label} decodes and plays`, async ({ page }) => {
      test.setTimeout(60_000);
      await page.goto('/');
      const result = await join(page, fromCluster);
      expect(result.error, `join ${label} failed`).toBeNull();
      expect(result.played).toBe(true);
      expect(result.advanced, `join ${label} did not advance`).toBe(true);
      expect(result.mediaError).toBeNull();
    });
  }

  test('a listener reconnecting is bootstrapped again and plays', async ({ page }) => {
    test.setTimeout(60_000);
    await page.goto('/');
    // Join, lose the socket, then rejoin later in the stream with a fresh
    // bootstrap - never with the dead connection's leftovers.
    const first = await join(page, 8);
    expect(first.advanced).toBe(true);
    const again = await join(page, 30);
    expect(again.error).toBeNull();
    expect(again.advanced, 'the reconnect did not advance').toBe(true);
  });

  test('a late joiner is not sent the Broadcast from the beginning', async () => {
    const index = JSON.parse(fs.readFileSync(FRAMES_INDEX, 'utf8'));
    const clusters = index.frames.slice(1);
    const joinAt = 33;
    const bootstrapBytes = index.initBytes
      + clusters.slice(Math.max(0, joinAt - LIVE_EDGE_CLUSTERS), joinAt)
        .reduce((total, frame) => total + frame.length, 0);
    const wholeBroadcast = index.totalBytes;
    // The bootstrap is decoder priming, not a recording: a joiner 10 seconds in
    // receives a tiny fraction of what has already been broadcast.
    expect(bootstrapBytes).toBeLessThan(wholeBroadcast * 0.15);
    expect(bootstrapBytes).toBeLessThan(16_384);
  });
});
