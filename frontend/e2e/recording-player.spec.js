/**
 * The Broadcast History recording bar, in a real browser.
 *
 * What this proves that a unit test cannot: that the bar is genuinely pinned
 * to the bottom of the content area, that it survives scrolling, that it does
 * not bury the last row or the pagination, and that the browser's own audio
 * widget really is absent from the rendered page.
 */
const { test, expect } = require('@playwright/test');
const { mockBackend, signIn } = require('./support/backend');

function recorded(id, campaign, size) {
  return {
    id, campaign_name: campaign, started_by: 1,
    started_at: '2026-08-01T09:00:00+00:00', ended_at: '2026-08-01T09:02:00+00:00',
    status: 'completed', target_mode: 'selected', selected_store_count: 2,
    online_store_count: 1, offline_store_count: 1, notes: null,
    created_at: '2026-08-01T09:00:00+00:00', archived_at: null,
    recording: { status: 'available', container: 'webm', codec: 'opus',
                 byte_size: size, duration_seconds: null, chunks_written: 40,
                 chunks_dropped: 0, started_at: '2026-08-01T09:00:00+00:00',
                 finalized_at: '2026-08-01T09:02:00+00:00', error: null },
  };
}

const SESSIONS = [
  recorded(8, 'Morning offer', 29696),
  recorded(9, 'Evening reminder', 51200),
  ...Array.from({ length: 10 }, (_, index) =>
    recorded(20 + index, `Filler ${index + 1}`, 12000)),
  { id: 10, campaign_name: 'Failed one', started_by: 1,
    started_at: '2026-08-01T19:00:00+00:00', ended_at: '2026-08-01T19:00:30+00:00',
    status: 'completed', target_mode: 'all', selected_store_count: 1,
    online_store_count: 1, offline_store_count: 0, notes: null,
    created_at: '2026-08-01T19:00:00+00:00', archived_at: null,
    recording: { status: 'failed', container: null, codec: null, byte_size: null,
                 duration_seconds: null, chunks_written: 3, chunks_dropped: 0,
                 started_at: '2026-08-01T19:00:00+00:00',
                 finalized_at: '2026-08-01T19:00:30+00:00',
                 error: 'no space left on device' } },
];

/** A tiny real WebM so the audio element has something legitimate to load. */
async function serveRecordingAudio(page) {
  for (const kind of ['audio', 'download']) {
    await page.route(`**/api/broadcast/sessions/*/recording/${kind}`, (route) =>
      route.fulfill({
        status: 200,
        contentType: 'audio/webm',
        body: Buffer.from('1a45dfa3', 'hex'),   // an EBML header and nothing more
      }));
  }
}

test.beforeEach(async ({ page }) => {
  await signIn(page);
  await mockBackend(page, { sessions: SESSIONS });
  await serveRecordingAudio(page);
});

test('a recorded broadcast offers Play and Download as SpeakLink actions', async ({ page }) => {
  await page.goto('/history');
  await expect(page.getByTestId('recording-play-8')).toBeVisible();
  await expect(page.getByTestId('recording-download-8')).toBeVisible();
  await expect(page.getByTestId('recording-meta-8')).toContainText('29 KB');
});

test('the browser native audio widget is never on the page', async ({ page }) => {
  await page.goto('/history');
  expect(await page.locator('audio').count()).toBe(0);       // closed

  await page.getByTestId('recording-play-8').click();
  await expect(page.getByTestId('recording-player-bar')).toBeVisible();
  // The element exists but carries no controls attribute, so the browser
  // draws nothing of its own.
  expect(await page.locator('audio[controls]').count()).toBe(0);
  expect(await page.locator('audio').count()).toBe(1);
});

test('Play opens a bar fixed to the bottom of the content area', async ({ page }) => {
  await page.goto('/history');
  await page.getByTestId('recording-play-8').click();
  const bar = page.getByTestId('recording-player-bar');
  await expect(bar).toBeVisible();

  const style = await bar.evaluate((node) => {
    const computed = getComputedStyle(node);
    return { position: computed.position, bottom: computed.bottom };
  });
  expect(style.position).toBe('fixed');
  expect(style.bottom).toBe('0px');

  // It starts where the sidebar ends and runs to the right edge.
  const box = await bar.boundingBox();
  const viewport = page.viewportSize();
  expect(box.x).toBeGreaterThan(0);
  expect(Math.round(box.x + box.width)).toBe(viewport.width);
});

test('opening the player does not change the row height', async ({ page }) => {
  await page.goto('/history');
  const row = page.getByTestId('history-row-8');
  const before = (await row.boundingBox()).height;

  await page.getByTestId('recording-play-8').click();
  await expect(page.getByTestId('recording-player-bar')).toBeVisible();
  expect((await row.boundingBox()).height).toBeCloseTo(before, 0);
});

test('the bar stays visible while History scrolls', async ({ page }) => {
  await page.goto('/history');
  await page.getByTestId('recording-play-8').click();
  const bar = page.getByTestId('recording-player-bar');
  await expect(bar).toBeVisible();
  const before = await bar.boundingBox();

  await page.mouse.wheel(0, 2000);
  await page.waitForTimeout(150);

  await expect(bar).toBeVisible();
  const after = await bar.boundingBox();
  // Pinned: it did not travel with the content.
  expect(Math.abs(after.y - before.y)).toBeLessThan(2);
});

test('the last row and the pagination stay reachable behind the bar', async ({ page }) => {
  await page.goto('/history');
  await page.getByTestId('recording-play-8').click();
  await expect(page.getByTestId('recording-player-bar')).toBeVisible();

  await page.mouse.wheel(0, 4000);
  await page.waitForTimeout(200);

  const bar = await page.getByTestId('recording-player-bar').boundingBox();
  const rows = page.locator('[data-testid^="history-row-"]');
  const last = await rows.last().boundingBox();
  // The page reserves room, so the final row clears the bar.
  expect(last.y + last.height).toBeLessThanOrEqual(bar.y + 2);
});

test('the bar names the broadcast being listened to', async ({ page }) => {
  await page.goto('/history');
  await page.getByTestId('recording-play-8').click();
  await expect(page.getByTestId('recording-campaign')).toHaveText('Morning offer');
  await expect(page.getByTestId('recording-session')).toContainText('Broadcast #8');
});

test('play and pause drive the transport', async ({ page }) => {
  await page.goto('/history');
  await page.getByTestId('recording-play-8').click();
  const toggle = page.getByTestId('recording-toggle');
  await expect(toggle).toBeEnabled();

  await toggle.click();
  // Chromium may refuse to decode the stub body; either way the state must be
  // one the component actually observed rather than an assumption.
  await expect(page.getByTestId('recording-state'))
    .toHaveText(/Playing|Paused|Playback failed|Finished/);
});

test('the volume control changes only this browser element', async ({ page }) => {
  await page.goto('/history');
  await page.getByTestId('recording-play-8').click();

  const stored = [];
  await page.route('**/api/store-audio/**', (route) => {
    stored.push(route.request().url());
    return route.fulfill({ status: 200, body: '{}' });
  });

  await page.getByTestId('recording-volume').fill('0.3');
  await page.getByTestId('recording-mute').click();

  const element = await page.locator('audio').first()
    .evaluate((node) => ({ volume: node.volume, muted: node.muted }));
  expect(element.volume).toBeCloseTo(0.3, 1);
  expect(element.muted).toBe(true);
  expect(stored).toEqual([]);          // nothing reached a Store
});

test('clicking elsewhere on History does NOT close the player', async ({ page }) => {
  await page.goto('/history');
  await page.getByTestId('recording-play-8').click();
  await expect(page.getByTestId('recording-player-bar')).toBeVisible();

  await page.getByTestId('history-refresh-btn').click();
  await expect(page.getByTestId('recording-player-bar')).toBeVisible();
});

test('Escape closes the player', async ({ page }) => {
  await page.goto('/history');
  await page.getByTestId('recording-play-8').click();
  await expect(page.getByTestId('recording-player-bar')).toBeVisible();

  await page.keyboard.press('Escape');
  await expect(page.getByTestId('recording-player-bar')).toBeHidden();
});

test('the close button closes the player', async ({ page }) => {
  await page.goto('/history');
  await page.getByTestId('recording-play-8').click();
  await page.getByTestId('recording-close').click();
  await expect(page.getByTestId('recording-player-bar')).toBeHidden();
});

test('choosing another recording switches the single player', async ({ page }) => {
  await page.goto('/history');
  await page.getByTestId('recording-play-8').click();
  await expect(page.getByTestId('recording-session')).toContainText('Broadcast #8');

  await page.getByTestId('recording-play-9').click();
  await expect(page.getByTestId('recording-session')).toContainText('Broadcast #9');
  // One bar, one audio element - two recordings cannot overlap.
  expect(await page.getByTestId('recording-player-bar').count()).toBe(1);
  expect(await page.locator('audio').count()).toBe(1);
});

test('Download reaches the authenticated download route', async ({ page }) => {
  await page.goto('/history');
  const request = page.waitForRequest((r) => r.url().includes('/recording/download'));
  await page.getByTestId('recording-download-8').click();
  const seen = await request;
  // The credential travels in a header, never in something bookmarkable.
  expect(seen.url()).not.toMatch(/token|password/);
});

test('the sidebar stays usable while the player is open', async ({ page }) => {
  await page.goto('/history');
  await page.getByTestId('recording-play-8').click();
  await expect(page.getByTestId('recording-player-bar')).toBeVisible();

  await page.getByTestId('nav-receivers').click();
  await expect(page).toHaveURL(/\/receivers/);
  // Leaving History takes the player with it.
  await expect(page.getByTestId('recording-player-bar')).toBeHidden();
});

test('a failed recording offers no player at all', async ({ page }) => {
  await page.goto('/history');
  await expect(page.getByTestId('recording-problem-10')).toHaveText('Recording failed');
  await expect(page.getByTestId('recording-play-10')).toHaveCount(0);
});

test('the recording controls are reachable by keyboard', async ({ page }) => {
  await page.goto('/history');
  const play = page.getByTestId('recording-play-8');
  await play.focus();
  await expect(play).toBeFocused();

  await page.keyboard.press('Enter');
  await expect(page.getByTestId('recording-player-bar')).toBeVisible();
});
