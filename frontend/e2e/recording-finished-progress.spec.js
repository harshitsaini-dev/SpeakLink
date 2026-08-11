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
 * THE TWO DEFECTS THAT SURVIVED THAT FIX
 *
 * The fill reached the end, but the POINT still stopped short - because what
 * was being seen was still the native thumb, whose centre travels inside an
 * inset equal to half its width, measured at 6px on a 12px thumb. No amount of
 * styling reaches inside the widget, so the visible thumb is now our own
 * element, positioned by the same percentage as the fill.
 *
 * And progress advanced in visible steps because it was driven only by
 * `timeupdate`, which Chromium fires roughly every 265ms - measured in this
 * player, not assumed. A frame loop now samples audio.currentTime instead; the
 * media element is still the clock, and nothing advances by wall time.
 *
 * These tests measure rendered pixels: the fill's right edge and the thumb's
 * CENTRE against the track. Geometry, not state.
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


// ===========================================================================
// The visual thumb, and smooth movement
// ===========================================================================
// The fill reaching the end was only half the defect. The POINT still stopped
// short, because the thing being seen was the native range thumb - whose centre
// travels inside an inset equal to half its width, measured at 6px. That is
// unfixable from outside the widget, so the thumb that is seen is now our own
// element positioned by the same percentage as the fill.
//
// And progress advanced in steps because it was driven by `timeupdate`, which
// Chromium fires roughly every 265ms - measured here, not assumed.

/** Track, fill and thumb geometry as the browser actually painted them. */
async function barGeometry(page) {
  return page.evaluate(() => {
    const box = (id) => document
      .querySelector(`[data-testid="${id}"]`).getBoundingClientRect();
    const track = box('recording-seek-track');
    const fill = box('recording-seek-fill');
    const thumb = box('recording-seek-thumb');
    return {
      trackLeft: track.left, trackRight: track.right, trackWidth: track.width,
      fillRight: fill.right,
      thumbCentre: thumb.left + thumb.width / 2,
      fillGap: track.right - fill.right,
      thumbGap: track.right - (thumb.left + thumb.width / 2),
    };
  });
}

for (const [label, id, seconds] of [['a 2 second', 8, 2], ['a 13 second', 9, 13]]) {
  test(`${label} recording ends with the fill AND the point at the very end`,
       async ({ page }) => {
    test.setTimeout(60_000);
    await page.goto('/history');
    await page.getByTestId(`recording-play-${id}`).click();
    await expect(page.getByTestId('recording-state')).toHaveText('Playing');

    await page.locator('[data-testid="recording-audio"]').evaluate(
      (node, target) => { node.currentTime = target; }, Math.max(0, seconds - 0.6));
    await expect(page.getByTestId('recording-state'))
      .toHaveText('Finished', { timeout: 20_000 });

    const geometry = await barGeometry(page);
    // The line reaches the end...
    expect(Math.abs(geometry.fillGap),
           `fill stops ${geometry.fillGap.toFixed(2)}px short`).toBeLessThanOrEqual(1);
    // ...and so does the CENTRE of the point, which is what the operator saw
    // lagging behind. The circle itself overhangs by half its width, which is
    // correct: the centre is the position.
    expect(Math.abs(geometry.thumbGap),
           `thumb centre stops ${geometry.thumbGap.toFixed(2)}px short`)
      .toBeLessThanOrEqual(1);
  });
}

test('the fill edge and the point stay together mid-playback', async ({ page }) => {
  test.setTimeout(60_000);
  await page.goto('/history');
  await page.getByTestId('recording-play-9').click();
  await expect(page.getByTestId('recording-state')).toHaveText('Playing');

  await page.locator('[data-testid="recording-audio"]')
    .evaluate((node) => { node.currentTime = 6.5; });      // half of 13s
  await page.waitForTimeout(300);

  const geometry = await barGeometry(page);
  // One value drives both, so they cannot drift apart.
  expect(Math.abs(geometry.fillRight - geometry.thumbCentre)).toBeLessThanOrEqual(1);
  // And both sit around the halfway mark of the real track.
  const fraction = (geometry.thumbCentre - geometry.trackLeft) / geometry.trackWidth;
  expect(fraction).toBeGreaterThan(0.4);
  expect(fraction).toBeLessThan(0.6);
});

test('progress moves smoothly rather than in timeupdate-sized jumps',
     async ({ page }) => {
  test.setTimeout(60_000);
  await page.goto('/history');
  await page.getByTestId('recording-play-9').click();
  await expect(page.getByTestId('recording-state')).toHaveText('Playing');

  // Roughly one second of samples, faster than timeupdate's ~265ms.
  const samples = [];
  for (let taken = 0; taken < 11; taken += 1) {
    samples.push((await barGeometry(page)).thumbCentre);
    await page.waitForTimeout(100);
  }

  const distinct = new Set(samples.map((value) => value.toFixed(1)));
  // Driven by timeupdate this would produce about four positions in a second.
  expect(distinct.size,
         `only ${distinct.size} distinct positions: ${samples.map((v) => v.toFixed(1))}`)
    .toBeGreaterThanOrEqual(5);

  // Forward, and never backwards.
  expect(samples[samples.length - 1]).toBeGreaterThan(samples[0]);
  for (let index = 1; index < samples.length; index += 1) {
    expect(samples[index]).toBeGreaterThanOrEqual(samples[index - 1] - 0.5);
  }
});

test('seeking jumps straight to the requested position', async ({ page }) => {
  test.setTimeout(60_000);
  await page.goto('/history');
  await page.getByTestId('recording-play-9').click();
  await expect(page.getByTestId('recording-state')).toHaveText('Playing');

  await page.getByTestId('recording-seek').fill('9.1');     // ~70% of 13s
  const geometry = await barGeometry(page);
  const fraction = (geometry.thumbCentre - geometry.trackLeft) / geometry.trackWidth;
  expect(fraction).toBeGreaterThan(0.6);
  expect(fraction).toBeLessThan(0.8);
  expect(Math.abs(geometry.fillRight - geometry.thumbCentre)).toBeLessThanOrEqual(1);

  const real = await page.locator('[data-testid="recording-audio"]')
    .evaluate((node) => node.currentTime);
  expect(real).toBeGreaterThan(8.5);
  expect(real).toBeLessThan(10);

  // And it keeps going from there.
  const before = (await barGeometry(page)).thumbCentre;
  await page.waitForTimeout(700);
  expect((await barGeometry(page)).thumbCentre).toBeGreaterThan(before);
});

test('pausing stops the point dead', async ({ page }) => {
  test.setTimeout(60_000);
  await page.goto('/history');
  await page.getByTestId('recording-play-9').click();
  await expect(page.getByTestId('recording-state')).toHaveText('Playing');
  await page.waitForTimeout(600);

  await page.getByTestId('recording-toggle').click();
  await expect(page.getByTestId('recording-state')).toHaveText('Paused');
  const paused = (await barGeometry(page)).thumbCentre;

  // No frame loop left running, so no drift.
  await page.waitForTimeout(600);
  expect(Math.abs((await barGeometry(page)).thumbCentre - paused))
    .toBeLessThanOrEqual(1);
});

test('replaying a finished recording resets both and advances again',
     async ({ page }) => {
  test.setTimeout(60_000);
  await page.goto('/history');
  await page.getByTestId('recording-play-8').click();
  await expect(page.getByTestId('recording-state')).toHaveText('Playing');
  await page.locator('[data-testid="recording-audio"]')
    .evaluate((node) => { node.currentTime = 1.4; });
  await expect(page.getByTestId('recording-state'))
    .toHaveText('Finished', { timeout: 20_000 });

  const finished = await barGeometry(page);
  expect(Math.abs(finished.fillGap)).toBeLessThanOrEqual(1);
  expect(Math.abs(finished.thumbGap)).toBeLessThanOrEqual(1);

  await page.getByTestId('recording-toggle').click();
  await expect(page.getByTestId('recording-state')).toHaveText('Playing');

  // An intentional jump back, not an animation from 100% to 0%.
  await expect.poll(async () => {
    const now = await barGeometry(page);
    return (now.thumbCentre - now.trackLeft) / now.trackWidth;
  }, { timeout: 10_000 }).toBeLessThan(0.2);

  const restarted = (await barGeometry(page)).thumbCentre;
  await page.waitForTimeout(600);
  expect((await barGeometry(page)).thumbCentre).toBeGreaterThan(restarted);
});

test('switching from a finished recording does not inherit its full bar',
     async ({ page }) => {
  test.setTimeout(60_000);
  await page.goto('/history');
  await page.getByTestId('recording-play-8').click();
  await expect(page.getByTestId('recording-state')).toHaveText('Playing');
  await page.locator('[data-testid="recording-audio"]')
    .evaluate((node) => { node.currentTime = 1.4; });
  await expect(page.getByTestId('recording-state'))
    .toHaveText('Finished', { timeout: 20_000 });

  // One click, as the accepted switching behaviour requires.
  await page.getByTestId('recording-play-9').click();
  await expect(page.getByTestId('recording-session')).toContainText('Broadcast #9');
  await expect(page.getByTestId('recording-state')).toHaveText('Playing');

  const geometry = await barGeometry(page);
  expect((geometry.thumbCentre - geometry.trackLeft) / geometry.trackWidth)
    .toBeLessThan(0.5);

  const truth = await page.locator('[data-testid="recording-audio"]')
    .evaluate(async (node) => {
      const start = node.currentTime;
      await new Promise((resolve) => setTimeout(resolve, 800));
      return { paused: node.paused, advanced: node.currentTime > start };
    });
  expect(truth.paused).toBe(false);
  expect(truth.advanced).toBe(true);
});

test('playback and its progress survive changing page', async ({ page }) => {
  test.setTimeout(60_000);
  await page.goto('/history');
  await page.getByTestId('recording-play-9').click();
  await expect(page.getByTestId('recording-state')).toHaveText('Playing');
  await page.waitForTimeout(500);
  const before = (await barGeometry(page)).thumbCentre;

  // The footer player lives above the router, so cleanup must not be tied to
  // the page that started the recording.
  await page.getByTestId('nav-receivers').click();
  await expect(page).toHaveURL(/\/receivers/);
  await expect(page.getByTestId('recording-player-bar')).toBeVisible();
  await expect(page.getByTestId('recording-session')).toContainText('Broadcast #9');
  await expect(page.getByTestId('recording-state')).toHaveText('Playing');

  // Still advancing, and still smoothly.
  const samples = [];
  for (let taken = 0; taken < 6; taken += 1) {
    samples.push((await barGeometry(page)).thumbCentre);
    await page.waitForTimeout(100);
  }
  expect(samples[samples.length - 1]).toBeGreaterThan(before);
  expect(new Set(samples.map((value) => value.toFixed(1))).size)
    .toBeGreaterThanOrEqual(3);
});
