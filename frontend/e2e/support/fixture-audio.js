/**
 * A real, seekable Opus/WebM for the recording tests - generated, never
 * committed.
 *
 * The repository refuses to track audio of any kind, and a synthetic tone is
 * no exception: `test_repository_contains_no_committed_audio_artifact` fails
 * the moment a .webm appears in `git ls-files`. So the fixture is built by
 * FFmpeg on demand and left in an ignored directory.
 *
 * It is remuxed exactly the way the backend now finalizes a recording, so
 * Chromium sees the finite duration a real finished recording carries.
 */
const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const DIRECTORY = path.join(__dirname, '..', 'fixtures');
const FIXTURE = path.join(DIRECTORY, 'recording-13s.webm');

function ensureRecordingFixture() {
  if (fs.existsSync(FIXTURE) && fs.statSync(FIXTURE).size > 0) {
    return fs.readFileSync(FIXTURE);
  }
  fs.mkdirSync(DIRECTORY, { recursive: true });
  const raw = path.join(DIRECTORY, 'raw-13s.webm');
  execFileSync('ffmpeg', ['-v', 'error', '-y', '-f', 'lavfi',
    '-i', 'sine=frequency=440:duration=13',
    '-c:a', 'libopus', '-b:a', '32k', '-ac', '1', '-f', 'webm', raw]);
  // The same stream copy the backend performs at finalization: it is what
  // gives the container a duration and an index.
  execFileSync('ffmpeg', ['-v', 'error', '-y', '-i', raw,
    '-c', 'copy', '-f', 'webm', FIXTURE]);
  fs.unlinkSync(raw);
  return fs.readFileSync(FIXTURE);
}

module.exports = { ensureRecordingFixture };
