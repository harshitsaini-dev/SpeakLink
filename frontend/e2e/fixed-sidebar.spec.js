/**
 * The SpeakLink navigation is FIXED to the desktop viewport. Always.
 *
 * This is a product contract, not an observation. It was `md:sticky`, which
 * keeps the element in normal flow and leaves its position dependent on which
 * ancestor happens to scroll - correct today, and quietly dependent on a
 * layout relationship any future page could change. Fixed takes it out of flow
 * and anchors it to the viewport, so no page, modal or live state can move it.
 *
 * Every assertion here measures getBoundingClientRect(). `toBeVisible()` is not
 * enough: a sidebar that has scrolled halfway up the screen is still visible.
 */
const { test, expect } = require('@playwright/test');
const { mockBackend, signIn } = require('./support/backend');

/** Tall enough that every page has something to scroll. */
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

/** One finished Broadcast with a recording, so the footer player really opens. */
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

const ROOM = {
  public_code: 'SL-AAA111', status: 'OPEN', auto_approve: false, delivery: 'ok',
  password: 'P-1', password_configured: true, password_rotated_at: null,
  counts: { waiting: 3, admitted: 8, connected: 8, listening: 6, buffering: 2, paused: 0 },
  waiting: Array.from({ length: 3 }, (_, i) => ({
    id: 500 + i, display_name: `Waiting ${i + 1}`,
    admission_status: 'REQUESTED', requested_at: '10:32:12' })),
  listeners: Array.from({ length: 8 }, (_, i) => ({
    id: 600 + i, display_name: `Listener ${i + 1}`, admitted_by: 'password',
    admission_status: 'PASSWORD_ADMITTED', playback_state: 'LISTENING',
    connected: true, seconds_since_seen: 1, stale: false })),
};

async function geometry(page) {
  return page.evaluate(() => {
    const aside = document.querySelector('aside');
    const main = document.querySelector('main');
    const box = aside.getBoundingClientRect();
    return {
      top: box.top, left: box.left, height: box.height, width: box.width,
      position: getComputedStyle(aside).position,
      documentScroll: document.documentElement.scrollTop,
      bodyScroll: document.body.scrollTop,
      windowScrollY: window.scrollY,
      mainScroll: main ? main.scrollTop : null,
      mainLeft: main ? main.getBoundingClientRect().left : null,
      horizontalOverflow:
        document.documentElement.scrollWidth > window.innerWidth + 1,
    };
  });
}

async function scrollMainFarDown(page) {
  await page.evaluate(() => {
    window.scrollBy(0, 10_000);
    const main = document.querySelector('main');
    if (main) main.scrollTop = main.scrollHeight;
  });
  await page.waitForTimeout(250);
}

/** The property under test, stated once. */
function expectPinned(before, after, label) {
  expect(after.position, `${label}: not fixed`).toBe('fixed');
  expect(Math.abs(after.top - before.top),
         `${label}: moved ${(after.top - before.top).toFixed(2)}px vertically`)
    .toBeLessThanOrEqual(1);
  expect(Math.abs(after.left - before.left), `${label}: moved horizontally`)
    .toBeLessThanOrEqual(1);
  expect(Math.abs(after.height - before.height), `${label}: changed height`)
    .toBeLessThanOrEqual(1);
  // The document must never become the scroller, or a fixed sidebar is the
  // only thing standing between the operator and a lost menu.
  expect(after.documentScroll, `${label}: the document scrolled`).toBe(0);
  expect(after.windowScrollY, `${label}: the window scrolled`).toBe(0);
  expect(after.horizontalOverflow, `${label}: horizontal overflow`).toBe(false);
}

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

// ===========================================================================
// Desktop viewport matrix
// ===========================================================================

for (const [width, height] of [[1920, 1080], [1600, 900], [1440, 900],
                               [1366, 768], [1280, 720], [1024, 768]]) {
  test(`the navigation is pinned at ${width}x${height}`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await mockBackend(page, { stores: FLEET });
    await page.goto('/console');
    await expect(page.getByTestId('stores-search')).toBeVisible();

    const before = await geometry(page);
    await scrollMainFarDown(page);
    const after = await geometry(page);

    console.log(`SIDEBAR ${width}x${height}: before top=${before.top} left=${before.left} `
      + `height=${before.height} position=${before.position} | after top=${after.top} `
      + `left=${after.left} height=${after.height} position=${after.position} `
      + `mainScrollTop=${after.mainScroll}`);

    expectPinned(before, after, `${width}x${height}`);
    // It really is the full viewport, and main really did scroll.
    expect(Math.abs(after.height - height)).toBeLessThanOrEqual(1);
    expect(after.mainScroll).toBeGreaterThan(0);
    // Main begins exactly where the sidebar ends - no overlap, no gap.
    expect(Math.abs(after.mainLeft - (before.left + before.width)))
      .toBeLessThanOrEqual(1);
  });
}

// ===========================================================================
// Every authenticated route
// ===========================================================================

const ROUTES = [
  ['/console', 'stores-search'],
  ['/active-broadcasts', 'active-page-info'],
  ['/stores', null],
  ['/history', null],
  ['/receivers', null],
  ['/devices', null],
  ['/logs', null],
  ['/users', null],
  ['/account/password', null],
];

for (const [route, marker] of ROUTES) {
  test(`the navigation is pinned on ${route}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 800 });
    await mockBackend(page, { stores: FLEET });
    await page.goto(route);
    if (marker) await expect(page.getByTestId(marker)).toBeVisible();
    await expect(page.getByTestId('app-sidebar')).toBeVisible();

    const before = await geometry(page);
    await scrollMainFarDown(page);
    const after = await geometry(page);
    expectPinned(before, after, route);

    // And still usable, not merely still there.
    await expect(page.getByTestId('nav-console')).toBeVisible();
    await expect(page.getByTestId('logout-btn')).toBeVisible();
  });
}

// ===========================================================================
// Live Console states
// ===========================================================================

for (const [label, current] of [['Selected Stores', LIVE_SELECTED],
                                ['Only With Link', LIVE_LINK_ONLY]]) {
  test(`the navigation is pinned during a LIVE ${label} Broadcast`,
       async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 800 });
    await mockBackend(page, { stores: FLEET, current });
    await page.route('**/api/broadcast/sessions/*/web-room', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json',
                      body: JSON.stringify(ROOM) }));
    await page.goto('/console');
    await expect(page.getByTestId('web-audience-panel')).toBeVisible();

    // Sampled through the scroll, not only at the ends: a sidebar that jumps
    // and returns would pass a before/after check.
    const before = await geometry(page);
    const samples = [];
    for (const fraction of [0, 0.25, 0.5, 0.75, 1]) {
      await page.evaluate((f) => {
        const main = document.querySelector('main');
        main.scrollTop = main.scrollHeight * f;
      }, fraction);
      await page.waitForTimeout(120);
      samples.push(await geometry(page));
    }

    for (const [index, sample] of samples.entries()) {
      expectPinned(before, sample, `${label} at sample ${index}`);
    }
  });
}

test('the navigation stays put while the confirmation modal is open',
     async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 800 });
  await mockBackend(page, { stores: FLEET });
  await page.goto('/console');
  await page.getByTestId('campaign-name-input').fill('Evening announcement');
  await page.getByTestId('target-mode-select').selectOption('only_with_link');

  const before = await geometry(page);
  await page.getByTestId('start-broadcast-btn').click();
  await expect(page.getByTestId('confirm-modal')).toBeVisible();

  const withModal = await geometry(page);
  await scrollMainFarDown(page);
  const after = await geometry(page);

  // A backdrop may block CLICKS. It may not move the sidebar.
  expectPinned(before, withModal, 'modal open');
  expectPinned(before, after, 'modal open after scroll');
});

// ===========================================================================
// Short viewport, player, mobile
// ===========================================================================

test('a short viewport scrolls only the navigation list', async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 600 });
  await mockBackend(page, { stores: FLEET });
  await page.goto('/console');
  await expect(page.getByTestId('app-sidebar')).toBeVisible();

  const zones = await page.evaluate(() => {
    const aside = document.querySelector('aside');
    const nav = aside.querySelector('nav');
    const logout = document.querySelector('[data-testid="logout-btn"]');
    return {
      asideHeight: aside.getBoundingClientRect().height,
      navOverflow: getComputedStyle(nav).overflowY,
      logoutBottom: logout.getBoundingClientRect().bottom,
      brandTop: aside.firstElementChild.getBoundingClientRect().top,
      viewport: window.innerHeight,
    };
  });

  expect(Math.abs(zones.asideHeight - 600)).toBeLessThanOrEqual(1);
  // Only the middle list scrolls; the brand and log out are pinned in place.
  expect(zones.navOverflow).toBe('auto');
  expect(zones.brandTop).toBeLessThanOrEqual(1);
  expect(zones.logoutBottom).toBeLessThanOrEqual(600 + 1);
});

test('the navigation and the recording player do not disturb each other',
     async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 800 });
  await mockBackend(page, { stores: FLEET, sessions: RECORDED });
  // A real audio body, so the player opens rather than erroring.
  await page.route('**/api/broadcast/sessions/*/recording/audio', (route) =>
    route.fulfill({ status: 200, contentType: 'audio/webm',
                    body: Buffer.from('1a45dfa3', 'hex') }));
  await page.goto('/history');

  await page.getByTestId('recording-play-8').click();
  await expect(page.getByTestId('recording-player-bar')).toBeVisible();

  const before = await geometry(page);
  await scrollMainFarDown(page);
  const after = await geometry(page);
  expectPinned(before, after, 'with the player open');

  const bar = await page.evaluate(() => {
    const overlaps = (a, b) => !(a.right <= b.left || b.right <= a.left
                                 || a.bottom <= b.top || b.bottom <= a.top);
    const aside = document.querySelector('aside').getBoundingClientRect();
    const player = document.querySelector('[data-testid="recording-player-bar"]')
      .getBoundingClientRect();
    const logout = document.querySelector('[data-testid="logout-btn"]')
      .getBoundingClientRect();
    return {
      asideRight: aside.right, playerLeft: player.left,
      // Real rectangle intersection. Comparing only vertical edges would call
      // these a collision when they sit in different columns, which is what
      // the sidebar offset exists to arrange.
      playerCoversLogout: overlaps(player, logout),
      playerCoversSidebar: overlaps(player, aside),
    };
  });
  // The player starts exactly where the navigation ends...
  expect(Math.abs(bar.playerLeft - bar.asideRight)).toBeLessThanOrEqual(1);
  // ...so it covers neither the sidebar nor Log out.
  expect(bar.playerCoversSidebar).toBe(false);
  expect(bar.playerCoversLogout).toBe(false);
});

test('a phone keeps its off-canvas drawer', async ({ page }) => {
  await page.setViewportSize({ width: 500, height: 800 });
  await mockBackend(page, { stores: FLEET });
  await page.goto('/console');
  await page.waitForSelector('aside', { state: 'attached' });

  const closed = await geometry(page);
  expect(closed.position).toBe('fixed');
  // Off-canvas, and no desktop offset forced onto the content.
  expect(closed.left).toBeLessThan(0);
  expect(closed.horizontalOverflow).toBe(false);
  expect(Math.abs(closed.mainLeft)).toBeLessThanOrEqual(1);

  await page.getByTestId('sidebar-toggle-btn').click();
  await page.waitForTimeout(350);
  const opened = await geometry(page);
  expect(opened.left).toBe(0);
  await expect(page.getByTestId('nav-console')).toBeVisible();
});
