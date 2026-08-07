/**
 * A finished recording must LOOK finished.
 *
 * The operator's report: at the end of a recording both timestamps read the
 * same, and the red line still stopped a few pixels short of the right-hand end.
 *
 * THE ROOT CAUSE, measured rather than guessed
 *
 * The seek control was a native `<input type="range">` with an accent colour.
 * Chromium paints that fill up to the THUMB CENTRE, and a thumb's travel is
 * inset by half its width at each end - so at value === max the fill stops
 * about half a thumb short. Worse, that fill is painted inside the widget:
 * there is no DOM node for it, so it could be neither corrected nor measured.
 * A test asserting `value === max` would have passed the whole time the
 * operator was looking at the gap.
 *
 * A second, smaller contribution: the last `timeupdate` fires before the end,
 * so currentTime at `ended` is routinely tens of milliseconds below duration.
 *
 * So the fill is now a real element behind a transparent range, and these tests
 * measure its right edge against the track's. Geometry, not state.
 */
const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');
const { mockBackend, signIn } = require('./support/backend');
const { ensureRecordingFixture } = require('./support/fixture-audio');

const DIRECTORY = path.join(__dirname, 'fixtures');
const SHORT_FIXTURE = path.join(DIRECTORY, 'recording-2s.webm');

/** A ~2 second finalized recording, so the end arrives quickly. */
function ensureShortFixture() {
  if (fs.existsSync(SHORT_FIXTURE) && fs.statSync(SHORT_FIXTURE).size > 0) {
    return fs.readFileSync(SHORT_FIXTURE);
  }
  fs.mkdirSync(DIRECTORY, { recursive: true });
  const raw = path.join(DIRECTORY, 'raw-2s.webm');
  execFileSync('ffmpeg', ['-v', 'error', '-y', '-f', 'lavfi',
    '-i', 'sine=frequency=440:duration=2',
    '-c:a', 'libopus', '-b:a', '32k', '-ac', '1', '-f', 'webm', raw]);
  // The same stream copy the backend performs at finalization, which is what
  // gives the container a duration.
  execFileSync('ffmpeg', ['-v', 'error', '-y', '-i', raw,
    '-c', 'copy', '-f', 'webm', SHORT_FIXTURE]);
  fs.unlinkSync(raw);
  return fs.readFileSync(SHORT_FIXTURE);
}

const LONG = ensureRecordingFixture();
const SHORT = ensureShortFixture();

function recorded(id, campaign, seconds) {
  return {
    id, campaign_name: campaign, started_by: 1,
    started_at: '2026-08-01T09:00:00+00:00', ended_at: '2026-08-01T09:02:00+00:00',
    status: 'completed', target_mode: 'selected', selected_store_count: 1,
    online_store_count: 1, offline_store_count: 0, notes: null,
    created_at: '2026-08-01T09:00:00+00:00', archived_at: null,
    recording: { status: 'available', container: 'webm', codec: 'opus',
                 byte_size: 40000, duration_seconds: seconds, chunks_written: 10,
                 chunks_dropped: 0, started_at: '2026-08-01T09:00:00+00:00',
                 finalized_at: '2026-08-01T09:02:00+00:00', error: null },
  };
}

const SESSIONS = [recorded(8, 'Two second clip', 2.0),
                  recorded(9, 'Thirteen second clip', 13.0)];

async function serve(page) {
  for (const kind of ['audio', 'download']) {
    await page.route(`**/api/broadcast/sessions/*/recording/${kind}`, (route) => {
      const id = route.request().url().match(/sessions\/(\d+)\//)[1];
      return route.fulfill({ status: 200, contentType: 'audio/webm',
                             body: id === '8' ? SHORT : LONG });
    });
  }
}

/** How far the played fill reaches, against the track it sits in. */
async function fillGeometry(page) {
  return page.evaluate(() => {
    const track = document.querySelector('[data-testid="recording-seek-track"]');
    const fill = document.querySelector('[data-testid="recording-seek-fill"]');
    const trackBox = track.getBoundingClientRect();
    const fillBox = fill.getBoundingClientRect();
    return {
      trackRight: trackBox.right,
      fillRight: fillBox.right,
      gap: trackBox.right - fillBox.right,
      trackWidth: trackBox.width,
      fillWidth: fillBox.width,
      styleWidth: fill.style.width,
    };
  });
}

test.beforeEach(async ({ page }) => {
  await signIn(page);
  await mockBackend(page, { sessions: SESSIONS });
  await serve(page);
});

for (const [label, id, seconds] of [['a 2 second', 8, 2], ['a 13 second', 9, 13]]) {
  test(`${label} recording finishes with the bar exactly full`, async ({ page }) => {
    test.setTimeout(60_000);
    await page.goto('/history');
    await page.getByTestId(`recording-play-${id}`).click();
    await expect(page.getByTestId('recording-state')).toHaveText('Playing');

    // Skip most of the clip rather than listening through it; the point is the
    // END, and the last second is what produces the real `ended` event.
    await page.locator('[data-testid="recording-audio"]').evaluate(
      (node, target) => { node.currentTime = target; }, Math.max(0, seconds - 0.6));

    // The REAL event, not a timer that assumed it happened.
    await expect(page.getByTestId('recording-state'))
      .toHaveText('Finished', { timeout: 20_000 });

    const geometry = await fillGeometry(page);
    expect(geometry.styleWidth).toBe('100%');
    // The visual property the operator actually reported.
    expect(Math.abs(geometry.gap),
           `the fill stops ${geometry.gap.toFixed(2)}px short of the track`)
      .toBeLessThanOrEqual(1);
  });
}

test('the timestamps stay truthful while the bar is full', async ({ page }) => {
  test.setTimeout(60_000);
  await page.goto('/history');
  await page.getByTestId('recording-play-8').click();
  await expect(page.getByTestId('recording-state')).toHaveText('Playing');
  await page.locator('[data-testid="recording-audio"]')
    .evaluate((node) => { node.currentTime = 1.4; });
  await expect(page.getByTestId('recording-state'))
    .toHaveText('Finished', { timeout: 20_000 });

  // The bar is painted full; the clock still reports what the element says.
  expect((await fillGeometry(page)).styleWidth).toBe('100%');
  const duration = await page.getByTestId('recording-duration').textContent();
  const position = await page.getByTestId('recording-position').textContent();
  expect(duration.trim()).toBe('0:02');
  expect(position.trim()).toBe('0:02');
  const real = await page.locator('[data-testid="recording-audio"]')
    .evaluate((node) => ({ current: node.currentTime, duration: node.duration }));
  expect(real.duration).toBeGreaterThan(1.5);
  expect(real.current).toBeGreaterThan(1.5);
});

test('replaying a finished recording empties the bar and refills it', async ({ page }) => {
  test.setTimeout(60_000);
  await page.goto('/history');
  await page.getByTestId('recording-play-9').click();
  await expect(page.getByTestId('recording-state')).toHaveText('Playing');
  await page.locator('[data-testid="recording-audio"]')
    .evaluate((node) => { node.currentTime = 12.5; });
  await expect(page.getByTestId('recording-state'))
    .toHaveText('Finished', { timeout: 20_000 });
  expect((await fillGeometry(page)).styleWidth).toBe('100%');

  await page.getByTestId('recording-toggle').click();
  await expect(page.getByTestId('recording-state')).toHaveText('Playing');

  // No stuck 100%: the bar reports the real new position...
  await expect.poll(async () => parseFloat((await fillGeometry(page)).styleWidth),
                    { timeout: 10_000 }).toBeLessThan(50);
  // ...and starts advancing again.
  const first = parseFloat((await fillGeometry(page)).styleWidth);
  await page.waitForTimeout(1200);
  expect(parseFloat((await fillGeometry(page)).styleWidth)).toBeGreaterThan(first);
});

test('switching to another recording does not inherit a full bar', async ({ page }) => {
  test.setTimeout(60_000);
  await page.goto('/history');
  await page.getByTestId('recording-play-8').click();
  await expect(page.getByTestId('recording-state')).toHaveText('Playing');
  await page.locator('[data-testid="recording-audio"]')
    .evaluate((node) => { node.currentTime = 1.4; });
  await expect(page.getByTestId('recording-state'))
    .toHaveText('Finished', { timeout: 20_000 });
  expect((await fillGeometry(page)).styleWidth).toBe('100%');

  // The accepted one-click switch, unchanged.
  await page.getByTestId('recording-play-9').click();
  await expect(page.getByTestId('recording-session')).toContainText('Broadcast #9');
  await expect(page.getByTestId('recording-state')).toHaveText('Playing');

  // A stale 100% would paint the new recording as already over.
  expect(parseFloat((await fillGeometry(page)).styleWidth)).toBeLessThan(50);
  const truth = await page.locator('[data-testid="recording-audio"]')
    .evaluate(async (node) => {
      const start = node.currentTime;
      await new Promise((resolve) => setTimeout(resolve, 800));
      return { paused: node.paused, advanced: node.currentTime > start };
    });
  expect(truth.paused).toBe(false);
  expect(truth.advanced).toBe(true);
});

test('the seek control is still a real keyboard-operable input', async ({ page }) => {
  await page.goto('/history');
  await page.getByTestId('recording-play-9').click();
  await expect(page.getByTestId('recording-seek')).toBeEnabled();

  const seek = page.getByTestId('recording-seek');
  await expect(seek).toHaveAttribute('aria-label', 'Seek');
  // The custom fill sits BEHIND a real range; the interaction was not traded
  // away for the paint.
  await seek.focus();
  const before = await seek.inputValue();
  await page.keyboard.press('ArrowRight');
  expect(await seek.inputValue()).not.toBe(before);
});
