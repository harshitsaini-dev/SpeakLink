const { test, expect } = require('@playwright/test');
const {
  mockBackend,
  signIn,
  instrumentMicrophone,
  stubWebSocket,
  removeOpusSupport,
} = require('./support/backend');

const CAMPAIGN = 'One-Store Bluetooth Amplifier Live Test';

/** Configure the console exactly as the amplifier live test was configured. */
async function selectOnlyStoreUN(page) {
  await expect(page.getByTestId('target-mode-select')).toHaveValue('selected');
  await page.getByTestId('stores-search').fill('Uttam Nagar Old');
  await page.getByTestId('store-checkbox-UN').check();
  await page.getByTestId('campaign-name-input').fill(CAMPAIGN);
}

async function goLive(page) {
  await page.getByTestId('start-broadcast-btn').click();
  await expect(page.getByTestId('confirm-modal')).toBeVisible();
  await page.getByTestId('confirm-start-btn').click();
}

test.describe('Broadcast Console targeting', () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page);
  });

  test('defaults to Selected Stores so a broadcast is never network-wide by accident', async ({ page }) => {
    await mockBackend(page);
    await page.goto('/console');
    await expect(page.getByTestId('target-mode-select')).toHaveValue('selected');
    await expect(page.getByTestId('stat-selected')).toContainText('0');
  });

  test('selecting exactly Store UN gives Selected 1, Online 1, Offline 0', async ({ page }) => {
    await mockBackend(page);
    await page.goto('/console');
    await selectOnlyStoreUN(page);

    await expect(page.getByTestId('stat-selected')).toContainText('1');
    await expect(page.getByTestId('stat-online')).toContainText('1');
    await expect(page.getByTestId('stat-offline')).toContainText('0');
  });

  test('the counters are real: selecting every Store reports the offline ones', async ({ page }) => {
    // During the live test the console showed Selected = 44 by accident. This
    // proves the counter reflects the true selection rather than a constant.
    await mockBackend(page);
    await page.goto('/console');
    await page.getByTestId('select-all-filtered-btn').click();

    await expect(page.getByTestId('stat-selected')).toContainText('3');
    await expect(page.getByTestId('stat-online')).toContainText('1');
    await expect(page.getByTestId('stat-offline')).toContainText('2');
  });

  test('Clear removes every selection', async ({ page }) => {
    await mockBackend(page);
    await page.goto('/console');
    await page.getByTestId('select-all-filtered-btn').click();
    await expect(page.getByTestId('stat-selected')).toContainText('3');

    await page.getByTestId('clear-selection-btn').click();
    await expect(page.getByTestId('stat-selected')).toContainText('0');
  });
});

test.describe('Start is gated', () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page);
    await mockBackend(page);
  });

  test('Start is disabled with no Store selected', async ({ page }) => {
    await page.goto('/console');
    await page.getByTestId('campaign-name-input').fill(CAMPAIGN);
    await expect(page.getByTestId('start-broadcast-btn')).toBeDisabled();
  });

  test('Start is disabled with no campaign name', async ({ page }) => {
    await page.goto('/console');
    await page.getByTestId('store-checkbox-UN').check();
    await expect(page.getByTestId('start-broadcast-btn')).toBeDisabled();
  });

  test('Start is enabled only once both are set, and asks for confirmation first', async ({ page }) => {
    await page.goto('/console');
    await selectOnlyStoreUN(page);
    await expect(page.getByTestId('start-broadcast-btn')).toBeEnabled();

    await page.getByTestId('start-broadcast-btn').click();
    // Clicking Start must not start anything; it opens a confirmation.
    const modal = page.getByTestId('confirm-modal');
    await expect(modal).toBeVisible();
    await expect(modal).toContainText(CAMPAIGN);
    await expect(modal).toContainText('Selected Stores');
    await expect(modal).toContainText('1 online, 0 offline');
  });

  test('Cancel on the confirmation starts nothing and opens no microphone', async ({ page }) => {
    await instrumentMicrophone(page);
    await page.goto('/console');
    await selectOnlyStoreUN(page);
    await page.getByTestId('start-broadcast-btn').click();
    await page.getByTestId('confirm-cancel-btn').click();

    await expect(page.getByTestId('confirm-modal')).toHaveCount(0);
    expect(await page.evaluate(() => window.__micCalls)).toBe(0);
  });
});

test.describe('READY gates the microphone', () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page);
  });

  test('no microphone is opened while no Receiver reports READY', async ({ page }) => {
    // The single most important assertion in this suite. READY is not implied
    // by a Store being ONLINE, by a session existing, or by prepare having been
    // sent - only by an explicit receiver_ready acknowledgement reaching the
    // backend and appearing in ready_receivers.
    test.setTimeout(60_000);

    await instrumentMicrophone(page);
    await stubWebSocket(page);
    await mockBackend(page, {
      current: { live: false, session: null, targets: [], ready_receivers: [] },
    });

    await page.goto('/console');
    await selectOnlyStoreUN(page);
    await goLive(page);

    // waitForReceiverReady polls for 20 s before giving up.
    await expect(page.getByTestId('console-error')).toContainText('No Receiver reported READY', {
      timeout: 40_000,
    });
    expect(await page.evaluate(() => window.__micCalls)).toBe(0);
  });

  test('a Store that is merely ONLINE does not count as READY', async ({ page }) => {
    test.setTimeout(60_000);

    await instrumentMicrophone(page);
    await stubWebSocket(page);
    // Store UN is online in the store list, but ready_receivers stays empty.
    await mockBackend(page, {
      current: { live: false, session: null, targets: [], ready_receivers: [] },
    });

    await page.goto('/console');
    // StatusBadge uppercases with CSS only; the DOM text stays lowercase.
    await expect(page.getByTestId('store-status-UN')).toContainText(/online/i);
    await selectOnlyStoreUN(page);
    await goLive(page);

    await expect(page.getByTestId('console-error')).toContainText('No Receiver reported READY', {
      timeout: 40_000,
    });
    expect(await page.evaluate(() => window.__micCalls)).toBe(0);
  });

  test('the microphone opens once a Receiver acknowledges READY', async ({ page }) => {
    await instrumentMicrophone(page);
    await stubWebSocket(page);
    await mockBackend(page, {
      current: { live: false, session: null, targets: [], ready_receivers: [1] },
    });

    await page.goto('/console');
    await selectOnlyStoreUN(page);
    await goLive(page);

    await expect.poll(() => page.evaluate(() => window.__micCalls)).toBe(1);
  });

  test('a denied microphone is reported and no recording starts', async ({ page }) => {
    await instrumentMicrophone(page, { deny: true });
    await stubWebSocket(page);
    await mockBackend(page, {
      current: { live: false, session: null, targets: [], ready_receivers: [1] },
    });

    await page.goto('/console');
    await selectOnlyStoreUN(page);
    await goLive(page);

    await expect(page.getByTestId('console-error')).toBeVisible();
    expect(await page.evaluate(() => window.__recorderStops)).toBe(0);
  });

  test('a browser without WebM/Opus is refused before the microphone is touched', async ({ page }) => {
    // SpeakLink must never silently send a different format to a Receiver whose
    // FFmpeg pipeline is configured for WebM/Opus.
    await instrumentMicrophone(page);
    await stubWebSocket(page);
    await removeOpusSupport(page);
    await mockBackend(page, {
      current: { live: false, session: null, targets: [], ready_receivers: [1] },
    });

    await page.goto('/console');
    await selectOnlyStoreUN(page);
    await goLive(page);

    await expect(page.getByTestId('console-error')).toContainText('WebM/Opus');
    expect(await page.evaluate(() => window.__micCalls)).toBe(0);
  });
});

test.describe('Play status is evidence, never inference', () => {
  const liveWith = (playStatus) => ({
    live: true,
    session: { id: 8, campaign_name: CAMPAIGN, started_at: new Date().toISOString(), status: 'live' },
    targets: [{ store_id: 1, play_status: playStatus }],
    ready_receivers: [1],
  });

  /**
   * Reach a live session the way an operator does: pick the Store while the
   * console is idle, then let the backend report the session as live. Store
   * checkboxes are disabled once live, so the selection cannot be made after.
   */
  async function liveWithStoreUN(page, playStatus) {
    await signIn(page);
    const state = await mockBackend(page);
    await page.goto('/console');
    await page.getByTestId('stores-search').fill('Uttam Nagar Old');
    await page.getByTestId('store-checkbox-UN').check();
    state.current = liveWith(playStatus);
    await expect(page.getByTestId('live-indicator')).toBeVisible();
    return state;
  }

  test('a command that was merely sent shows PENDING, never PLAYING', async ({ page }) => {
    await liveWithStoreUN(page, 'pending');

    const status = page.getByTestId('play-status-UN');
    await expect(status).toContainText(/pending/i);
    await expect(status).not.toContainText(/playing/i);
  });

  test('AUDIO RECEIVING is shown only from the Receiver acknowledgement', async ({ page }) => {
    await liveWithStoreUN(page, 'audio_receiving');

    const status = page.getByTestId('play-status-UN');
    await expect(status).toContainText(/audio receiving/i);
    await expect(status).not.toContainText(/playback confirmed/i);
  });

  test('PLAYBACK CONFIRMED is a separate, later state', async ({ page }) => {
    await liveWithStoreUN(page, 'playback_confirmed');
    await expect(page.getByTestId('play-status-UN')).toContainText(/playback confirmed/i);
  });

  test('a playback error is shown as an error, not as silence', async ({ page }) => {
    await liveWithStoreUN(page, 'playback_error');
    await expect(page.getByTestId('play-status-UN')).toContainText(/playback error/i);
  });

  test('SPEAKER VERIFIED never appears anywhere in the console', async ({ page }) => {
    // It is not implemented. No amount of software evidence may imply that a
    // human or a microphone confirmed sound actually left a Store speaker.
    await liveWithStoreUN(page, 'playback_confirmed');
    await expect(page.getByTestId('play-status-UN')).toContainText(/playback confirmed/i);

    const body = (await page.textContent('body')) || '';
    expect(body.toUpperCase()).not.toContain('SPEAKER VERIFIED');
    expect(body.toUpperCase()).not.toContain('SPEAKER_VERIFIED');
  });
});

test.describe('The target summary counts real Receiver states', () => {
  // The console counted play_status === "playing" and play_status === "failed".
  // The backend writes neither: it writes pending, audio_receiving,
  // playback_confirmed, playback_error, device_error and stopped. Both counters
  // were therefore permanently zero. That erred safe - a merely-sent command
  // was never shown as Playing - but it also meant an operator watching a live
  // broadcast had no summary of what the Receivers were actually doing.
  async function liveWithTargets(page, targets) {
    await signIn(page);
    const state = await mockBackend(page);
    await page.goto('/console');
    await page.getByTestId('select-all-filtered-btn').click();
    state.current = {
      live: true,
      session: { id: 8, campaign_name: CAMPAIGN, started_at: new Date().toISOString(), status: 'live' },
      targets,
      ready_receivers: [1],
    };
    await expect(page.getByTestId('live-indicator')).toBeVisible();
    return state;
  }

  test('a target that is only pending counts as neither receiving nor confirmed', async ({ page }) => {
    await liveWithTargets(page, [{ store_id: 1, play_status: 'pending' }]);

    await expect(page.getByTestId('stat-audio-receiving')).toContainText('0');
    await expect(page.getByTestId('stat-playback-confirmed')).toContainText('0');
    await expect(page.getByTestId('stat-target-errors')).toContainText('0');
  });

  test('audio_receiving is counted as receiving, not as confirmed playback', async ({ page }) => {
    await liveWithTargets(page, [
      { store_id: 1, play_status: 'audio_receiving' },
      { store_id: 2, play_status: 'pending' },
    ]);

    await expect(page.getByTestId('stat-audio-receiving')).toContainText('1');
    await expect(page.getByTestId('stat-playback-confirmed')).toContainText('0');
  });

  test('playback_confirmed is counted separately', async ({ page }) => {
    await liveWithTargets(page, [
      { store_id: 1, play_status: 'playback_confirmed' },
      { store_id: 2, play_status: 'audio_receiving' },
      { store_id: 5, play_status: 'pending' },
    ]);

    await expect(page.getByTestId('stat-audio-receiving')).toContainText('1');
    await expect(page.getByTestId('stat-playback-confirmed')).toContainText('1');
  });

  test('playback_error and device_error are both counted as errors', async ({ page }) => {
    await liveWithTargets(page, [
      { store_id: 1, play_status: 'playback_error' },
      { store_id: 2, play_status: 'device_error' },
      { store_id: 5, play_status: 'playback_confirmed' },
    ]);

    await expect(page.getByTestId('stat-target-errors')).toContainText('2');
    await expect(page.getByTestId('stat-playback-confirmed')).toContainText('1');
  });

  test('the summary never uses the word Playing for a sent command', async ({ page }) => {
    await liveWithTargets(page, [{ store_id: 1, play_status: 'pending' }]);

    const summary = await page.getByTestId('target-summary').textContent();
    expect((summary || '').toLowerCase()).not.toContain('playing');
  });
});

test.describe('Stopping a broadcast', () => {
  test('Stop releases the microphone track and the recorder', async ({ page }) => {
    await signIn(page);
    await instrumentMicrophone(page);
    await stubWebSocket(page);
    const state = await mockBackend(page, {
      current: { live: false, session: null, targets: [], ready_receivers: [1] },
    });

    await page.goto('/console');
    await selectOnlyStoreUN(page);
    await goLive(page);
    await expect.poll(() => page.evaluate(() => window.__micCalls)).toBe(1);

    // The backend now reports the session as live, so the console offers Stop.
    state.current = {
      live: true,
      session: { id: 8, campaign_name: CAMPAIGN, started_at: new Date().toISOString(), status: 'live' },
      targets: [{ store_id: 1, play_status: 'playback_confirmed' }],
      ready_receivers: [1],
    };

    await page.getByTestId('stop-broadcast-btn').click();

    // Stopping the recorder is not enough: the microphone track must be
    // released too, or the browser keeps showing a live recording indicator.
    await expect.poll(() => page.evaluate(() => window.__recorderStops)).toBeGreaterThan(0);
    await expect.poll(() => page.evaluate(() => window.__trackStops)).toBeGreaterThan(0);
    expect(state.stopCalls.length).toBeGreaterThan(0);
  });
});
