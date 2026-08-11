/**
 * Main is the only vertical scroll surface in the HQ application.
 *
 * The reported symptom was two right-hand scrollbars: scrolling the outer one
 * carried the whole UI, header included, upward. html, body and #root had no
 * height or overflow rules at all, so they were height:auto / overflow:visible
 * and free to grow past the viewport beside main's own scroller.
 *
 * These tests use REAL mouse wheel input, because setting main.scrollTop by
 * hand proves only that main CAN scroll - not that the wheel goes there. And
 * they keep wheeling after main reaches its end, because that is the moment the
 * outer scroller used to take over.
 */
const { test, expect } = require('@playwright/test');
const { mockBackend, signIn } = require('./support/backend');

const FLEET = Array.from({ length: 40 }, (_, index) => ({
  id: index + 1,
  store_code: `S${String(index + 1).padStart(2, '0')}`,
  store_name: `Store ${index + 1}`,
  city: 'DELHI', region: 'NORTH',
  is_online_store: false, status: index % 3 === 0 ? 'online' : 'offline',
}));

const LIVE_SELECTED = {
  live: true,
  session: { id: 77, campaign_name: 'Evening announcement', target_mode: 'selected',
             started_at: '2026-08-07T09:00:00+00:00', status: 'live',
             selected_store_count: 40, online_store_count: 14, offline_store_count: 26 },
  targets: [],
};

const LIVE_LINK_ONLY = {
  live: true,
  session: { id: 78, campaign_name: 'Web only', target_mode: 'only_with_link',
             started_at: '2026-08-07T09:00:00+00:00', status: 'live',
             selected_store_count: 0, online_store_count: 0, offline_store_count: 0 },
  targets: [],
};

/** A deliberately large audience, so the middle card has plenty to hold. */
const BIG_ROOM = {
  public_code: 'SL-AAA111', status: 'OPEN', auto_approve: false, delivery: 'ok',
  password: 'P-1', password_configured: true, password_rotated_at: null,
  counts: { waiting: 12, admitted: 40, connected: 40, listening: 30,
            buffering: 10, paused: 0 },
  waiting: Array.from({ length: 12 }, (_, i) => ({
    id: 500 + i, display_name: `Waiting ${i + 1}`,
    admission_status: 'REQUESTED', requested_at: '10:32:12' })),
  listeners: Array.from({ length: 40 }, (_, i) => ({
    id: 600 + i, display_name: `Listener ${i + 1}`, admitted_by: 'password',
    admission_status: 'PASSWORD_ADMITTED', playback_state: 'LISTENING',
    connected: true, seconds_since_seen: 1, stale: false })),
};

const RECORDED = [{
  id: 8, campaign_name: 'Yesterday', started_by: 1,
  started_at: '2026-08-01T09:00:00+00:00', ended_at: '2026-08-01T09:02:00+00:00',
  status: 'completed', target_mode: 'selected', selected_store_count: 1,
  online_store_count: 1, offline_store_count: 0, notes: null,
  created_at: '2026-08-01T09:00:00+00:00', archived_at: null,
  recording: { status: 'available', container: 'webm', codec: 'opus',
               byte_size: 40000, duration_seconds: 13, chunks_written: 10,
               chunks_dropped: 0, started_at: '2026-08-01T09:00:00+00:00',
               finalized_at: '2026-08-01T09:02:00+00:00', error: null },
}];

/** Every layer that could own a scrollbar, measured rather than assumed. */
async function shellState(page) {
  return page.evaluate(() => {
    const layer = (el) => (el ? {
      clientHeight: el.clientHeight, scrollHeight: el.scrollHeight,
      scrollTop: el.scrollTop, overflowY: getComputedStyle(el).overflowY,
      scrolls: el.scrollHeight > el.clientHeight + 1,
    } : null);
    const rect = (selector) => {
      const node = document.querySelector(selector);
      if (!node) return null;
      const box = node.getBoundingClientRect();
      return { top: box.top, left: box.left, height: box.height };
    };
    const main = document.querySelector('[data-testid="app-main-scroll"]');
    return {
      html: layer(document.documentElement),
      body: layer(document.body),
      root: layer(document.getElementById('root')),
      shell: layer(document.querySelector('[data-testid="app-shell"]')),
      mainShell: layer(document.querySelector('[data-testid="app-main-shell"]')),
      main: layer(main),
      windowScrollY: window.scrollY,
      sidebar: rect('[data-testid="app-sidebar"]'),
      header: rect('[data-testid="app-header"]'),
      // Which ROOT-LEVEL elements can scroll vertically. Component-local
      // scrollers (the audience list, the sidebar nav) are excluded on
      // purpose - they are bounded areas, not the page.
      globalScrollers: ['html', 'body', '#root',
                        '[data-testid="app-shell"]',
                        '[data-testid="app-main-shell"]',
                        '[data-testid="app-main-scroll"]']
        .filter((selector) => {
          const node = selector === 'html' ? document.documentElement
            : selector === 'body' ? document.body
            : document.querySelector(selector);
          return node && node.scrollHeight > node.clientHeight + 1;
        }),
    };
  });
}

/** Real wheel input over the middle of the content. */
async function wheelOverMain(page, amount, times = 1) {
  const box = await page.locator('[data-testid="app-main-scroll"]').boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  for (let turn = 0; turn < times; turn += 1) {
    await page.mouse.wheel(0, amount);
    await page.waitForTimeout(120);
  }
  await page.waitForTimeout(250);
}

function expectOnlyMainScrolls(state, label) {
  expect(state.globalScrollers,
         `${label}: expected only main to scroll, got ${state.globalScrollers}`)
    .toEqual(['[data-testid="app-main-scroll"]']);
  expect(state.windowScrollY, `${label}: the window scrolled`).toBe(0);
  expect(state.html.scrollTop, `${label}: the document scrolled`).toBe(0);
  expect(state.body.scrollTop, `${label}: the body scrolled`).toBe(0);
  expect(state.html.scrollHeight).toBeLessThanOrEqual(state.html.clientHeight + 1);
  expect(state.body.scrollHeight).toBeLessThanOrEqual(state.body.clientHeight + 1);
  expect(state.root.scrollHeight).toBeLessThanOrEqual(state.root.clientHeight + 1);
}

async function openConsole(page, { current = null, room = BIG_ROOM } = {}) {
  await mockBackend(page, { stores: FLEET, ...(current ? { current } : {}) });
  await page.route('**/api/broadcast/sessions/*/web-room', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json',
                    body: JSON.stringify(room) }));
  await page.goto('/console');
  await expect(page.getByTestId('app-main-scroll')).toBeVisible();
}

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

// ===========================================================================
// The reported symptom
// ===========================================================================

test('wheeling past the bottom cannot move the application', async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 768 });
  await openConsole(page, { current: LIVE_SELECTED });

  const before = await shellState(page);
  expect(before.main.scrolls, 'the fixture is not tall enough to scroll').toBe(true);
  expectOnlyMainScrolls(before, 'before');

  await wheelOverMain(page, 2000, 3);
  const scrolled = await shellState(page);
  expect(scrolled.main.scrollTop, 'the wheel did not reach main').toBeGreaterThan(0);
  expectOnlyMainScrolls(scrolled, 'after wheeling');

  // To the very end, and then well past it - the moment the outer scroller
  // used to take over and carry the header away.
  await wheelOverMain(page, 5000, 6);
  const atBottom = await shellState(page);
  const maxScroll = atBottom.main.scrollHeight - atBottom.main.clientHeight;
  expect(Math.abs(atBottom.main.scrollTop - maxScroll)).toBeLessThanOrEqual(2);

  await wheelOverMain(page, 5000, 6);
  const past = await shellState(page);

  console.log('SHELL 1366x768:', JSON.stringify({
    html: `${before.html.clientHeight}/${before.html.scrollHeight}`,
    body: `${before.body.clientHeight}/${before.body.scrollHeight}`,
    root: `${before.root.clientHeight}/${before.root.scrollHeight}`,
    main: `${before.main.clientHeight}/${before.main.scrollHeight}`,
    mainScrollBefore: before.main.scrollTop,
    mainScrollAtBottom: atBottom.main.scrollTop,
    mainScrollPastBottom: past.main.scrollTop,
    windowY: past.windowScrollY,
  }));

  expectOnlyMainScrolls(past, 'past the bottom');
  // Clamped, not carried onward.
  expect(Math.abs(past.main.scrollTop - maxScroll)).toBeLessThanOrEqual(2);
  // And nothing moved.
  expect(past.sidebar.top).toBe(before.sidebar.top);
  expect(past.sidebar.left).toBe(before.sidebar.left);
  expect(past.header.top).toBe(before.header.top);
  expect(past.header.left).toBe(before.header.left);
});

// ===========================================================================
// Viewport matrix
// ===========================================================================

for (const [width, height] of [[1920, 1080], [1600, 900], [1440, 900],
                               [1366, 768], [1280, 720], [1024, 768]]) {
  test(`only main scrolls at ${width}x${height}`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await openConsole(page, { current: LIVE_SELECTED });

    const before = await shellState(page);
    await wheelOverMain(page, 3000, 3);
    const after = await shellState(page);

    expectOnlyMainScrolls(after, `${width}x${height}`);
    expect(after.main.scrollTop).toBeGreaterThan(0);
    expect(after.sidebar.top).toBe(before.sidebar.top);
    expect(after.header.top).toBe(before.header.top);
  });
}

// ===========================================================================
// States and routes
// ===========================================================================

test('an idle Console has one scroll owner', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 800 });
  await openConsole(page);
  await wheelOverMain(page, 3000, 2);
  expectOnlyMainScrolls(await shellState(page), 'idle Console');
});

test('a live Only With Link Console has one scroll owner', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 800 });
  await openConsole(page, { current: LIVE_LINK_ONLY });
  await wheelOverMain(page, 3000, 2);
  expectOnlyMainScrolls(await shellState(page), 'link-only live Console');
});

test('a large Web Audience does not create a second scrollbar', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 800 });
  await openConsole(page, { current: LIVE_SELECTED, room: BIG_ROOM });
  await expect(page.getByTestId('web-audience-panel')).toBeVisible();
  await wheelOverMain(page, 3000, 3);
  // Forty listeners scroll INSIDE their card; the page still has one scroller.
  expectOnlyMainScrolls(await shellState(page), 'large audience');
});

for (const route of ['/active-broadcasts', '/stores', '/history', '/receivers',
                     '/devices', '/logs', '/users', '/account/password']) {
  test(`only main scrolls on ${route}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 800 });
    await mockBackend(page, { stores: FLEET });
    await page.goto(route);
    await expect(page.getByTestId('app-main-scroll')).toBeVisible();

    await wheelOverMain(page, 3000, 2);
    const state = await shellState(page);
    // A short page legitimately scrolls nothing; a long one scrolls main only.
    expect(state.globalScrollers.filter(
      (s) => s !== '[data-testid="app-main-scroll"]'),
      `${route}: something outside main scrolls`).toEqual([]);
    expect(state.windowScrollY).toBe(0);
    expect(state.html.scrollTop).toBe(0);
  });
}

// ===========================================================================
// Modal and player
// ===========================================================================

test('the confirmation modal does not hand scrolling to the document',
     async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 800 });
  await openConsole(page);
  await page.getByTestId('campaign-name-input').fill('Evening announcement');
  await page.getByTestId('target-mode-select').selectOption('only_with_link');
  await page.getByTestId('start-broadcast-btn').click();
  await expect(page.getByTestId('confirm-modal')).toBeVisible();

  await page.mouse.move(720, 400);
  await page.mouse.wheel(0, 4000);
  await page.waitForTimeout(250);
  const state = await shellState(page);
  expect(state.windowScrollY).toBe(0);
  expect(state.html.scrollTop).toBe(0);
  expect(state.body.scrollTop).toBe(0);
});

test('the recording player does not create outer overflow', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 800 });
  await mockBackend(page, { stores: FLEET, sessions: RECORDED });
  await page.route('**/api/broadcast/sessions/*/recording/audio', (route) =>
    route.fulfill({ status: 200, contentType: 'audio/webm',
                    body: Buffer.from('1a45dfa3', 'hex') }));
  await page.goto('/history');
  await page.getByTestId('recording-play-8').click();
  await expect(page.getByTestId('recording-player-bar')).toBeVisible();

  await wheelOverMain(page, 3000, 3);
  const state = await shellState(page);
  // A fixed footer must not add to the document's height.
  expect(state.html.scrollHeight).toBeLessThanOrEqual(state.html.clientHeight + 1);
  expect(state.body.scrollHeight).toBeLessThanOrEqual(state.body.clientHeight + 1);
  expect(state.windowScrollY).toBe(0);
});

test('a phone still scrolls its content and nothing else', async ({ page }) => {
  await page.setViewportSize({ width: 500, height: 800 });
  await mockBackend(page, { stores: FLEET });
  await page.goto('/console');
  await expect(page.getByTestId('app-main-scroll')).toBeVisible();

  await wheelOverMain(page, 2000, 3);
  const state = await shellState(page);
  expect(state.windowScrollY).toBe(0);
  expect(state.html.scrollTop).toBe(0);
  expect(state.globalScrollers.filter(
    (s) => s !== '[data-testid="app-main-scroll"]')).toEqual([]);
});
