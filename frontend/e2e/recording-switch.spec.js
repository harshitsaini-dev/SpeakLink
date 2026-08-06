/**
 * Switching from one Broadcast recording to another, without closing first.
 *
 * THE DEFECT THIS FILE EXISTS FOR
 *
 * Recording A playing, click Play on B once, and the footer said B while
 * nothing was audible - sometimes neither A nor B. Closing the player and
 * then choosing B worked, which is exactly the shape of a source-transition
 * race rather than a wrong label.
 *
 * These assertions therefore never trust the footer text on its own. They ask
 * the audio element what it is actually playing, and whether its position is
 * advancing.
 */
const { test, expect } = require('@playwright/test');
const { mockBackend, signIn } = require('./support/backend');
const { ensureRecordingFixture } = require('./support/fixture-audio');

// Generated, never committed: the repository tracks no audio at all.
const FIXTURE = ensureRecordingFixture();

function recorded(id, campaign) {
  return {
    id, campaign_name: campaign, started_by: 1,
    started_at: '2026-08-01T09:00:00+00:00', ended_at: '2026-08-01T09:02:00+00:00',
    status: 'completed', target_mode: 'selected', selected_store_count: 1,
    online_store_count: 1, offline_store_count: 0, notes: null,
    created_at: '2026-08-01T09:00:00+00:00', archived_at: null,
    recording: { status: 'available', container: 'webm', codec: 'opus',
                 byte_size: 77638, duration_seconds: 13.008, chunks_written: 40,
                 chunks_dropped: 0, started_at: '2026-08-01T09:00:00+00:00',
                 finalized_at: '2026-08-01T09:02:00+00:00', error: null },
  };
}

const SESSIONS = [recorded(8, 'Recording A'), recorded(9, 'Recording B'),
                  recorded(11, 'Recording C')];

/** Serve the real fixture, optionally delaying particular sessions. */
async function serveAudio(page, delays = {}) {
  await page.route('**/api/broadcast/sessions/*/recording/audio', async (route) => {
    const id = route.request().url().match(/sessions\/(\d+)\//)[1];
    const wait = delays[id] || 0;
    if (wait) await new Promise((resolve) => setTimeout(resolve, wait));
    return route.fulfill({ status: 200, contentType: 'audio/webm', body: FIXTURE });
  });
  await page.route('**/api/broadcast/sessions/*/recording/download', (route) =>
    route.fulfill({ status: 200, contentType: 'audio/webm', body: FIXTURE }));
}

/** What the element is REALLY doing, not what the footer claims. */
async function playbackTruth(page) {
  return page.locator('audio').first().evaluate(async (node) => {
    const start = node.currentTime;
    await new Promise((resolve) => setTimeout(resolve, 700));
    return {
      paused: node.paused,
      advanced: node.currentTime > start,
      currentTime: node.currentTime,
      sessionId: node.getAttribute('data-active-session-id'),
    };
  });
}

test.beforeEach(async ({ page }) => {
  await signIn(page);
  await mockBackend(page, { sessions: SESSIONS });
});

test('A plays, then B plays, with no Close in between', async ({ page }) => {
  await serveAudio(page);
  await page.goto('/history');

  await page.getByTestId('recording-play-8').click();
  await expect(page.getByTestId('recording-state')).toHaveText('Playing');
  const first = await playbackTruth(page);
  expect(first.paused).toBe(false);
  expect(first.advanced).toBe(true);

  // Straight to B. No Close, one click.
  await page.getByTestId('recording-play-9').click();
  await expect(page.getByTestId('recording-session')).toContainText('Broadcast #9');
  await expect(page.getByTestId('recording-state')).toHaveText('Playing');

  const second = await playbackTruth(page);
  expect(second.paused).toBe(false);
  expect(second.advanced).toBe(true);
  // The element is playing B, not merely labelled B.
  expect(second.sessionId).toBe('9');
  expect(await page.locator('audio').count()).toBe(1);
});

test('switching back and forth keeps producing real playback', async ({ page }) => {
  await serveAudio(page);
  await page.goto('/history');

  for (const id of [8, 9, 8, 9]) {
    await page.getByTestId(`recording-play-${id}`).click();
    await expect(page.getByTestId('recording-session'))
      .toContainText(`Broadcast #${id}`);
    await expect(page.getByTestId('recording-state')).toHaveText('Playing');

    const truth = await playbackTruth(page);
    expect(truth.paused, `session ${id} was not playing`).toBe(false);
    expect(truth.advanced, `session ${id} did not advance`).toBe(true);
    expect(truth.sessionId).toBe(String(id));
  }
});

test('rapid A B C selection leaves only C playing', async ({ page }) => {
  // A is slow and B slower, so both land AFTER C. Neither may take over.
  await serveAudio(page, { 8: 1200, 9: 1800, 11: 50 });
  await page.goto('/history');

  await page.getByTestId('recording-play-8').click();
  await page.getByTestId('recording-play-9').click();
  await page.getByTestId('recording-play-11').click();

  await expect(page.getByTestId('recording-session')).toContainText('Broadcast #11');
  await expect(page.getByTestId('recording-state')).toHaveText('Playing');

  // Long enough for the slow responses to arrive and try to interfere.
  await page.waitForTimeout(2500);
  await expect(page.getByTestId('recording-session')).toContainText('Broadcast #11');

  const truth = await playbackTruth(page);
  expect(truth.sessionId).toBe('11');
  expect(truth.paused).toBe(false);
  expect(truth.advanced).toBe(true);
});

test('a switch whose audio cannot be fetched fails honestly', async ({ page }) => {
  await serveAudio(page);
  await page.goto('/history');
  await page.getByTestId('recording-play-8').click();
  await expect(page.getByTestId('recording-state')).toHaveText('Playing');

  await page.unroute('**/api/broadcast/sessions/*/recording/audio');
  await page.route('**/api/broadcast/sessions/*/recording/audio', (route) =>
    route.fulfill({ status: 500, body: 'nope' }));

  await page.getByTestId('recording-play-9').click();
  await expect(page.getByTestId('recording-session')).toContainText('Broadcast #9');
  // Nothing may claim to be playing B, and A must not carry on underneath.
  await expect(page.getByTestId('recording-state')).not.toHaveText('Playing');
  await expect(page.getByTestId('recording-bar-error')).toBeVisible();
  expect(await page.locator('audio').first()
    .evaluate((node) => node.paused)).toBe(true);
});

test('the same recording resumes rather than restarting', async ({ page }) => {
  await serveAudio(page);
  await page.goto('/history');
  await page.getByTestId('recording-play-8').click();
  await expect(page.getByTestId('recording-state')).toHaveText('Playing');
  await page.waitForTimeout(900);

  await page.getByTestId('recording-toggle').click();      // pause
  const paused = await page.locator('audio').first()
    .evaluate((node) => node.currentTime);
  expect(paused).toBeGreaterThan(0);

  await page.getByTestId('recording-play-8').click();       // ask again
  await expect(page.getByTestId('recording-state')).toHaveText('Playing');
  const resumed = await page.locator('audio').first()
    .evaluate((node) => node.currentTime);
  // Carried on from where it was, not restarted at zero.
  expect(resumed).toBeGreaterThanOrEqual(paused - 0.5);
});
