/**
 * Capture REAL Chromium MediaRecorder WebM/Opus bytes to disk.
 *
 * The backend Cluster framer must be tested against bytes a browser actually
 * produced, not against bytes a test hand-rolled to match the parser's own
 * assumptions - that would prove only that the parser agrees with itself.
 *
 * The repository refuses to track audio artifacts, so the capture is generated
 * on demand into a gitignored directory.
 */
const fs = require('fs');
const path = require('path');

const CAPTURE_DIR = path.join(__dirname, '..', '..', '..', 'backend', 'tests', 'fixtures');
const CAPTURE_FILE = path.join(CAPTURE_DIR, 'mediarecorder-live.webm');
const CHUNK_INDEX_FILE = path.join(CAPTURE_DIR, 'mediarecorder-live.chunks.json');

const MIME = 'audio/webm;codecs=opus';

/**
 * Record `durationMs` of real WebM/Opus and write both the concatenated stream
 * and the per-timeslice chunk boundaries, so backend tests can replay the
 * stream exactly as the broadcaster socket would deliver it.
 */
async function captureLiveWebm(page, { durationMs = 12_000, timesliceMs = 250 } = {}) {
  const captured = await page.evaluate(
    async ({ mime, durationMs: total, timesliceMs: slice }) => {
      if (!window.MediaRecorder || !MediaRecorder.isTypeSupported(mime)) return null;
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const recorder = new MediaRecorder(stream, {
        mimeType: mime, audioBitsPerSecond: 32000,
      });
      const blobs = [];
      recorder.ondataavailable = (event) => {
        if (event.data && event.data.size) blobs.push(event.data);
      };
      recorder.start(slice);
      await new Promise((resolve) => setTimeout(resolve, total));
      recorder.stop();
      stream.getTracks().forEach((track) => track.stop());
      await new Promise((resolve) => setTimeout(resolve, 250));

      const sizes = [];
      const parts = [];
      for (const blob of blobs) {
        const bytes = new Uint8Array(await blob.arrayBuffer());
        sizes.push(bytes.byteLength);
        parts.push(Array.from(bytes));
      }
      return { sizes, bytes: parts.flat() };
    },
    { mime: MIME, durationMs, timesliceMs },
  );

  if (!captured) throw new Error('this browser cannot record audio/webm;codecs=opus');

  fs.mkdirSync(CAPTURE_DIR, { recursive: true });
  fs.writeFileSync(CAPTURE_FILE, Buffer.from(captured.bytes));
  fs.writeFileSync(CHUNK_INDEX_FILE, JSON.stringify({
    mime: MIME, timesliceMs, chunkSizes: captured.sizes,
    totalBytes: captured.bytes.length,
  }, null, 2));
  return { file: CAPTURE_FILE, chunkSizes: captured.sizes };
}

module.exports = { captureLiveWebm, CAPTURE_FILE, CHUNK_INDEX_FILE, CAPTURE_DIR };
