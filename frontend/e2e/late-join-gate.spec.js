/**
 * THE LATE-JOIN PROTOTYPE GATE.
 *
 * A web listener who joins 30 seconds into a live Broadcast must hear the live
 * edge. Whether that is even possible is a property of the REAL MediaRecorder
 * WebM/Opus byte format, not of any code SpeakLink has written yet - so it is
 * settled here, against a real Chromium and a real MediaRecorder, before the
 * public feature is designed around an assumption.
 *
 * THE ASSUMPTION UNDER TEST
 *
 * A MediaRecorder started with a 250 ms timeslice emits a first Blob carrying
 * the EBML header, Segment and Tracks - the initialization segment - and then
 * further Blobs carrying media. If, and ONLY if, a fresh MediaSource can be fed
 * [first chunk] + [chunks from an arbitrary later point] and decode, a bounded
 * "cache the init segment, then send live clusters" relay is sufficient and no
 * per-listener transcode is needed.
 *
 * This test does not assert that it works. It MEASURES whether it does, and
 * reports the numbers, so the architecture that follows is chosen on evidence.
 *
 * WHAT IT MEASURED
 *
 * Of 113 non-initial chunks, ZERO began with a Cluster identifier. A 250 ms
 * timeslice boundary is therefore not a container boundary, and the naive
 * relay hands a SourceBuffer a partial cluster.
 *
 * Chromium tolerates that INTERMITTENTLY. Across repeated runs with identical
 * captured bytes, the 30-second timeslice-aligned join decoded on some runs and
 * failed with an append error on others. Intermittently is not support, so the
 * naive relay is rejected on evidence rather than on taste.
 *
 * Resuming from a genuine Cluster boundary, with the initialization segment
 * (every byte before the first Cluster) cached and sent first, decoded and
 * advanced on every offset and every repeat. That is what the backend relay
 * must do: split the broadcaster's byte stream on Cluster boundaries, cache the
 * one initialization segment per Broadcast, and start each listener at a real
 * cluster. It needs no FFmpeg process, no per-listener transcode and no WebRTC.
 */
const { test, expect } = require('@playwright/test');

test.describe.configure({ mode: 'serial' });

const MIME = 'audio/webm;codecs=opus';
const TIMESLICE_MS = 250;
const CAPTURE_MS = 34_000;

/**
 * Record real WebM/Opus for CAPTURE_MS, then attempt a late join at each of the
 * requested offsets by feeding a fresh MediaSource the init chunk plus every
 * chunk recorded from that offset onward.
 */
async function measureLateJoin(page, joinOffsetsMs) {
  return page.evaluate(
    async ({ mime, timeslice, captureMs, offsets }) => {
      const report = { supported: {}, chunks: [], joins: [] };
      report.supported.mediaRecorder =
        !!window.MediaRecorder && MediaRecorder.isTypeSupported(mime);
      report.supported.mediaSource =
        !!window.MediaSource && MediaSource.isTypeSupported(mime);
      if (!report.supported.mediaRecorder || !report.supported.mediaSource) {
        return report;
      }

      // ---- capture real live audio, exactly as HQBroadcaster does ----------
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const recorder = new MediaRecorder(stream, {
        mimeType: mime, audioBitsPerSecond: 32000,
      });
      const chunks = [];
      const startedAt = performance.now();
      recorder.ondataavailable = (event) => {
        if (event.data && event.data.size) {
          chunks.push({ at: performance.now() - startedAt, blob: event.data });
        }
      };
      recorder.start(timeslice);
      await new Promise((resolve) => setTimeout(resolve, captureMs));
      recorder.stop();
      stream.getTracks().forEach((track) => track.stop());
      await new Promise((resolve) => setTimeout(resolve, 200));

      const buffers = [];
      for (const chunk of chunks) buffers.push(new Uint8Array(await chunk.blob.arrayBuffer()));
      report.chunks = chunks.map((chunk, index) => ({
        index, at: Math.round(chunk.at), bytes: buffers[index].byteLength,
      }));

      // Which chunks BEGIN with an EBML/Cluster identifier tells us whether a
      // timeslice boundary is also a container boundary.
      const startsWith = (bytes, sig) => sig.every((byte, i) => bytes[i] === byte);
      report.firstChunkIsEbmlHeader = startsWith(buffers[0], [0x1a, 0x45, 0xdf, 0xa3]);
      report.laterChunksStartingWithCluster = buffers
        .slice(1)
        .filter((bytes) => startsWith(bytes, [0x1f, 0x43, 0xb6, 0x75])).length;
      report.laterChunkCount = buffers.length - 1;

      // ---- attempt a late join at each offset ------------------------------
      const attempt = async (offsetMs) => {
        // Chunk 0 IS the init segment and is always appended, so joining "at
        // the start" means resuming from chunk 1 rather than from chunk 0.
        const found = chunks.findIndex((chunk) => chunk.at >= offsetMs);
        const from = Math.max(found, 1);
        const outcome = {
          offsetMs, fromChunk: from,
          appended: 0, decodeError: null, bufferedStart: null,
          bufferedEnd: null, played: false, advanced: false,
        };
        if (found === -1) { outcome.decodeError = 'no chunk at that offset'; return outcome; }

        const audio = document.createElement('audio');
        audio.muted = true;                 // measuring decode, not audibility
        document.body.appendChild(audio);
        const source = new MediaSource();
        audio.src = URL.createObjectURL(source);

        try {
          await new Promise((resolve, reject) => {
            const failed = setTimeout(() => reject(new Error('sourceopen timeout')), 5000);
            source.addEventListener('sourceopen', () => { clearTimeout(failed); resolve(); },
                                    { once: true });
          });
          const buffer = source.addSourceBuffer(mime);
          // "sequence" rewrites timestamps, so a listener joining late starts
          // at zero on their own timeline rather than seeking 30s into nothing.
          buffer.mode = 'sequence';

          const append = (bytes) => new Promise((resolve, reject) => {
            buffer.addEventListener('updateend', resolve, { once: true });
            buffer.addEventListener('error', () => reject(new Error('append error')),
                                    { once: true });
            buffer.appendBuffer(bytes);
          });

          // The init segment, then the live edge onward. Nothing in between.
          await append(buffers[0]);
          outcome.appended += 1;
          for (let i = from; i < buffers.length; i += 1) {
            await append(buffers[i]);
            outcome.appended += 1;
          }

          if (buffer.buffered.length) {
            outcome.bufferedStart = buffer.buffered.start(0);
            outcome.bufferedEnd = buffer.buffered.end(buffer.buffered.length - 1);
          }

          await audio.play();
          outcome.played = true;
          const before = audio.currentTime;
          await new Promise((resolve) => setTimeout(resolve, 900));
          outcome.advanced = audio.currentTime > before;
          outcome.currentTime = audio.currentTime;
          outcome.mediaError = audio.error ? audio.error.code : null;
        } catch (error) {
          outcome.decodeError = String(error && error.message ? error.message : error);
        } finally {
          try { audio.pause(); } catch (ignored) { /* already gone */ }
          audio.remove();
        }
        return outcome;
      };

      // ---- the decisive comparison ----------------------------------------
      // Timeslice boundaries are demonstrably NOT cluster boundaries, so the
      // naive relay hands a SourceBuffer a partial cluster. Whether Chromium
      // tolerates that is the whole question, and tolerating it SOMETIMES is
      // the same as not supporting it. So each offset is attempted both ways,
      // and each way is attempted twice, in a fresh MediaSource every time.
      const CLUSTER = [0x1f, 0x43, 0xb6, 0x75];
      const whole = new Uint8Array(buffers.reduce((n, b) => n + b.byteLength, 0));
      const chunkStart = [];
      let cursor = 0;
      for (const bytes of buffers) {
        chunkStart.push(cursor);
        whole.set(bytes, cursor);
        cursor += bytes.byteLength;
      }
      const clusterOffsets = [];
      for (let i = 0; i + 4 <= whole.length; i += 1) {
        if (CLUSTER.every((byte, k) => whole[i + k] === byte)) clusterOffsets.push(i);
      }
      report.clusterCount = clusterOffsets.length;
      report.initSegmentBytes = clusterOffsets.length ? clusterOffsets[0] : null;

      /** Resume from the first genuine Cluster at or after a chunk index. */
      const clusterAlignedFrom = (chunkIndex) => {
        const byteOffset = chunkStart[chunkIndex];
        const cluster = clusterOffsets.find((offset) => offset >= byteOffset);
        if (cluster === undefined) return null;
        return { init: whole.slice(0, clusterOffsets[0]), media: whole.slice(cluster) };
      };

      const attemptAligned = async (offsetMs) => {
        const found = chunks.findIndex((chunk) => chunk.at >= offsetMs);
        const from = Math.max(found, 1);
        const parts = clusterAlignedFrom(from);
        const outcome = { offsetMs, alignment: 'cluster', fromChunk: from,
                          decodeError: null, played: false, advanced: false };
        if (!parts) { outcome.decodeError = 'no cluster at that offset'; return outcome; }

        const audio = document.createElement('audio');
        audio.muted = true;
        document.body.appendChild(audio);
        const source = new MediaSource();
        audio.src = URL.createObjectURL(source);
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
            buffer.appendBuffer(bytes);
          });
          await append(parts.init);
          await append(parts.media);
          if (buffer.buffered.length) {
            outcome.bufferedEnd = buffer.buffered.end(buffer.buffered.length - 1);
          }
          await audio.play();
          outcome.played = true;
          const before = audio.currentTime;
          await new Promise((resolve) => setTimeout(resolve, 900));
          outcome.advanced = audio.currentTime > before;
          outcome.mediaError = audio.error ? audio.error.code : null;
        } catch (error) {
          outcome.decodeError = String(error && error.message ? error.message : error);
        } finally {
          try { audio.pause(); } catch (ignored) { /* already gone */ }
          audio.remove();
        }
        return outcome;
      };

      for (const offset of offsets) {
        const naive = await attempt(offset);
        naive.alignment = 'timeslice';
        report.joins.push(naive);
        report.joins.push(await attemptAligned(offset));
      }
      return report;
    },
    { mime: MIME, timeslice: TIMESLICE_MS, captureMs: CAPTURE_MS, offsets: joinOffsetsMs },
  );
}

test('a late joiner can decode the live edge from a cached init segment', async ({ page }) => {
  test.setTimeout(180_000);
  await page.goto('/');

  // Join at the start, 5 seconds in, 30 seconds in, and once more after the
  // first attempt has already torn a MediaSource down - the reconnect case.
  const report = await measureLateJoin(page, [0, 5_000, 30_000, 30_000]);

  console.log('LATE-JOIN GATE REPORT\n' + JSON.stringify({
    supported: report.supported,
    chunkCount: report.chunks.length,
    firstChunkBytes: report.chunks[0] && report.chunks[0].bytes,
    medianLaterChunkBytes: report.chunks.length > 2
      ? report.chunks[Math.floor(report.chunks.length / 2)].bytes : null,
    firstChunkIsEbmlHeader: report.firstChunkIsEbmlHeader,
    laterChunksStartingWithCluster: report.laterChunksStartingWithCluster,
    laterChunkCount: report.laterChunkCount,
    joins: report.joins,
  }, null, 2));

  expect(report.supported.mediaRecorder).toBe(true);
  expect(report.supported.mediaSource).toBe(true);

  // A timeslice boundary is not a container boundary. If this ever becomes
  // false, the naive relay would be viable and this gate should be revisited.
  expect(report.laterChunksStartingWithCluster).toBe(0);

  // THE GATE: resuming from a genuine Cluster boundary must work at every
  // offset, including the repeat, with a cached initialization segment.
  const aligned = report.joins.filter((join) => join.alignment === 'cluster');
  expect(aligned.length).toBeGreaterThan(0);
  for (const join of aligned) {
    expect(join.decodeError, `cluster join at ${join.offsetMs}ms failed`).toBeNull();
    expect(join.played, `cluster join at ${join.offsetMs}ms did not start`).toBe(true);
    expect(join.advanced, `cluster join at ${join.offsetMs}ms did not advance`).toBe(true);
  }
});
