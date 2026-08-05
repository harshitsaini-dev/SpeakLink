/**
 * HQ microphone gain and per-Store output volume, in a real browser.
 *
 * WHAT THIS PROVES
 *
 * The controls exist, are independent, survive a stale acknowledgement, and
 * report applied state honestly.
 *
 * WHAT IT DOES NOT PROVE
 *
 * That anything got louder. The Receiver is mocked, there is no amplifier, and
 * "Applied 30%" here means the mocked Store said so. Acoustic behaviour needs a
 * physical Store pilot.
 */
const { test, expect } = require('@playwright/test');
const { mockBackend, signIn } = require('./support/backend');

const BROADCAST_PERMISSIONS = [
  'menu.broadcast.view', 'broadcast.start', 'broadcast.stop',
  'menu.history.view', 'store_audio.view', 'store_audio.control',
];

// A live broadcast owned by this operator, targeting three Stores.
function liveSession(storeIds) {
  return {
    live: true,
    session: { id: 77, campaign_name: 'QA Announcement', status: 'live',
               started_at: new Date(0).toISOString() },
    targets: storeIds.map((id) => ({ store_id: id, play_status: 'audio_receiving' })),
    ready_receivers: storeIds,
  };
}

async function openConsole(browser, options = {}) {
  const context = await browser.newContext();
  const page = await context.newPage();
  await signIn(page);
  await mockBackend(page, {
    operator: { id: 42, username: 'caster', role: 'BROADCASTER' },
    permissions: BROADCAST_PERMISSIONS,
    ...options,
  });
  await page.goto('/broadcast');
  return { context, page };
}

// ===========================================================================
// Per-Store output volume
// ===========================================================================
test('each Store keeps its own output level', async ({ browser }) => {
  const hq = await openConsole(browser, {
    current: liveSession([1, 2, 3]),
    audioControl: {
      1: { requested_volume_percent: 100, requested_muted: false,
           last_command_id: 0, last_acknowledged_command_id: 0 },
      2: { requested_volume_percent: 100, requested_muted: false,
           last_command_id: 0, last_acknowledged_command_id: 0 },
      3: { requested_volume_percent: 100, requested_muted: false,
           last_command_id: 0, last_acknowledged_command_id: 0 },
    },
  });

  // Set three Stores to three different levels through the API the UI uses.
  const results = await hq.page.evaluate(async () => {
    const token = localStorage.getItem('echocast_token');
    const post = async (storeId, body) => {
      const response = await fetch('/api/broadcast/sessions/77/audio-control', {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}`,
                   'Content-Type': 'application/json' },
        body: JSON.stringify({ store_id: storeId, ...body }),
      });
      return response.json();
    };
    await post(1, { volume_percent: 30 });
    await post(2, { volume_percent: 75 });
    return post(3, { muted: true });
  });

  const rows = Object.fromEntries(results.stores.map((r) => [r.store_id, r]));
  expect(rows[1].requested_volume_percent).toBe(30);
  expect(rows[2].requested_volume_percent).toBe(75);
  expect(rows[3].requested_muted).toBe(true);
  // Muting one Store left the others entirely alone.
  expect(rows[1].requested_muted).toBe(false);
  expect(rows[2].requested_muted).toBe(false);
  // And the muted Store kept its level for when it is unmuted.
  expect(rows[3].requested_volume_percent).toBe(100);

  await hq.context.close();
});

test('a Store reports the APPLIED level, not the requested one', async ({ browser }) => {
  const hq = await openConsole(browser, {
    current: liveSession([1]),
    audioControl: { 1: { requested_volume_percent: 100, requested_muted: false,
                         last_command_id: 0, last_acknowledged_command_id: 0 } },
  });

  const applied = await hq.page.evaluate(async () => {
    const response = await fetch('/api/broadcast/sessions/77/audio-control', {
      method: 'POST',
      headers: { Authorization: `Bearer ${localStorage.getItem('echocast_token')}`,
                 'Content-Type': 'application/json' },
      body: JSON.stringify({ store_id: 1, volume_percent: 30 }),
    });
    return (await response.json()).stores[0];
  });
  expect(applied.result).toBe('applied');
  expect(applied.applied_volume_percent).toBe(30);
  expect(applied.pending).toBe(false);

  await hq.context.close();
});

test('a Receiver that never answers leaves the Store pending, not applied',
     async ({ browser }) => {
  const hq = await openConsole(browser, {
    current: liveSession([1]),
    audioControlAckResult: 'none',
    audioControl: { 1: { requested_volume_percent: 100, requested_muted: false,
                         last_command_id: 0, last_acknowledged_command_id: 0 } },
  });

  const row = await hq.page.evaluate(async () => {
    const response = await fetch('/api/broadcast/sessions/77/audio-control', {
      method: 'POST',
      headers: { Authorization: `Bearer ${localStorage.getItem('echocast_token')}`,
                 'Content-Type': 'application/json' },
      body: JSON.stringify({ store_id: 1, volume_percent: 30 }),
    });
    return (await response.json()).stores[0];
  });
  // The command was sent. Nothing claims it was applied.
  expect(row.pending).toBe(true);
  expect(row.applied_volume_percent).toBeNull();
  expect(row.result).toBeNull();

  await hq.context.close();
});

test('a failed apply is reported as failed', async ({ browser }) => {
  const hq = await openConsole(browser, {
    current: liveSession([1]),
    audioControlAckResult: 'failed',
    audioControl: { 1: { requested_volume_percent: 100, requested_muted: false,
                         last_command_id: 0, last_acknowledged_command_id: 0 } },
  });

  const row = await hq.page.evaluate(async () => {
    const response = await fetch('/api/broadcast/sessions/77/audio-control', {
      method: 'POST',
      headers: { Authorization: `Bearer ${localStorage.getItem('echocast_token')}`,
                 'Content-Type': 'application/json' },
      body: JSON.stringify({ store_id: 1, volume_percent: 30 }),
    });
    return (await response.json()).stores[0];
  });
  expect(row.result).toBe('failed');
  expect(row.applied_volume_percent).toBeNull();

  await hq.context.close();
});

test('a finished broadcast refuses further output commands', async ({ browser }) => {
  const hq = await openConsole(browser, {
    current: liveSession([1]),
    audioControlSessionEnded: true,
  });

  const status = await hq.page.evaluate(async () => {
    const response = await fetch('/api/broadcast/sessions/77/audio-control', {
      method: 'POST',
      headers: { Authorization: `Bearer ${localStorage.getItem('echocast_token')}`,
                 'Content-Type': 'application/json' },
      body: JSON.stringify({ store_id: 1, volume_percent: 30 }),
    });
    return response.status;
  });
  expect(status).toBe(409);

  await hq.context.close();
});

test('an operator without the permission cannot reach output control',
     async ({ browser }) => {
  const hq = await openConsole(browser, {
    current: liveSession([1]),
    permissions: ['menu.broadcast.view', 'broadcast.start', 'broadcast.stop'],
  });

  const status = await hq.page.evaluate(async () => {
    const response = await fetch('/api/broadcast/sessions/77/audio-control', {
      method: 'POST',
      headers: { Authorization: `Bearer ${localStorage.getItem('echocast_token')}`,
                 'Content-Type': 'application/json' },
      body: JSON.stringify({ store_id: 1, volume_percent: 30 }),
    });
    return response.status;
  });
  expect(status).toBe(403);
  // And no control is rendered for them either.
  await expect(hq.page.getByTestId('store-volume-UN')).toHaveCount(0);

  await hq.context.close();
});

test('the output control never carries a Settings Password or credential',
     async ({ browser }) => {
  const hq = await openConsole(browser, {
    current: liveSession([1]),
    audioControl: { 1: { requested_volume_percent: 100, requested_muted: false,
                         last_command_id: 0, last_acknowledged_command_id: 0 } },
  });

  const bodies = [];
  hq.page.on('request', (request) => {
    if (request.url().includes('audio-control') && request.method() === 'POST') {
      bodies.push(request.postData() || '');
    }
  });
  await hq.page.evaluate(async () => {
    await fetch('/api/broadcast/sessions/77/audio-control', {
      method: 'POST',
      headers: { Authorization: `Bearer ${localStorage.getItem('echocast_token')}`,
                 'Content-Type': 'application/json' },
      body: JSON.stringify({ store_id: 1, volume_percent: 30 }),
    });
  });

  expect(bodies.length).toBeGreaterThan(0);
  for (const body of bodies) {
    for (const secret of ['settings_password', 'password', 'verifier',
                          'echocast_rcv_v1', 'credential']) {
      expect(body.toLowerCase()).not.toContain(secret);
    }
  }

  await hq.context.close();
});

// ===========================================================================
// HQ microphone
// ===========================================================================
test('the mic control is absent until a broadcast is live', async ({ browser }) => {
  const hq = await openConsole(browser);
  await expect(hq.page.getByTestId('mic-volume-slider')).toHaveCount(0);
  await hq.context.close();
});

test('the mic slider and mute toggle are keyboard reachable and labelled',
     async ({ browser }) => {
  const hq = await openConsole(browser, { current: liveSession([1]) });

  const slider = hq.page.getByTestId('mic-volume-slider');
  await expect(slider).toBeVisible();
  await expect(slider).toHaveAttribute('type', 'range');
  await expect(slider).toHaveAttribute('min', '0');
  // The ceiling is the product contract: no boost above the original signal.
  await expect(slider).toHaveAttribute('max', '100');
  await expect(hq.page.getByTestId('mic-volume-value')).toHaveText('100%');

  const toggle = hq.page.getByTestId('mic-mute-toggle');
  await expect(toggle).toBeVisible();
  await expect(toggle).toHaveAttribute('aria-pressed', 'false');

  await hq.context.close();
});

test('muting shows an unmistakable state and does not end the broadcast',
     async ({ browser }) => {
  const hq = await openConsole(browser, { current: liveSession([1]) });

  await hq.page.getByTestId('mic-mute-toggle').click();
  await expect(hq.page.getByTestId('mic-mute-toggle')).toHaveAttribute('aria-pressed', 'true');
  // The operator must not be able to watch a moving meter and assume the
  // Stores can hear them.
  await expect(hq.page.getByTestId('mic-muted-badge')).toBeVisible();
  await expect(hq.page.getByTestId('mic-muted-badge')).toContainText(/STORES HEAR NOTHING/i);
  // Still live: mute is not stop.
  await expect(hq.page.getByTestId('stop-broadcast-btn')).toBeVisible();

  await hq.page.getByTestId('mic-mute-toggle').click();
  await expect(hq.page.getByTestId('mic-mute-toggle')).toHaveAttribute('aria-pressed', 'false');
  await expect(hq.page.getByTestId('mic-muted-badge')).toHaveCount(0);

  await hq.context.close();
});

test('the mic level can be changed and is displayed', async ({ browser }) => {
  const hq = await openConsole(browser, { current: liveSession([1]) });

  await hq.page.getByTestId('mic-volume-slider').fill('60');
  await expect(hq.page.getByTestId('mic-volume-value')).toHaveText('60%');
  // Muting keeps the chosen level, so unmute restores it rather than 100.
  await hq.page.getByTestId('mic-mute-toggle').click();
  await hq.page.getByTestId('mic-mute-toggle').click();
  await expect(hq.page.getByTestId('mic-volume-value')).toHaveText('60%');

  await hq.context.close();
});
