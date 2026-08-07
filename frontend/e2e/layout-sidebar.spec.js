/**
 * The EchoCast navigation stays put while content scrolls.
 *
 * WHAT WAS REPORTED, AND WHAT WAS MEASURED
 *
 * Manual acceptance reported that scrolling a long Broadcast Console carried
 * the left navigation up and out of view. Measured against the current build at
 * 1920, 1440, 1280, 1024, 900, 820, 768 and 700 CSS pixels wide, it does not:
 * the aside reports viewport top 0 before and after scrolling, and the document
 * itself never scrolls at all - the layout is viewport-height with an
 * internally scrolling <main>.
 *
 * So this file does not fix that symptom; it PINS the behaviour, so that if a
 * future change to the shell ever does let the navigation scroll away, a test
 * says so rather than an operator. The reported case still needs a viewport
 * size, zoom level and page from the person who saw it.
 */
const { test, expect } = require('@playwright/test');
const { mockBackend, signIn } = require('./support/backend');

/** Enough Stores that the Console is far taller than any viewport. */
const FLEET = Array.from({ length: 40 }, (_, index) => ({
  id: index + 1,
  store_code: `S${String(index + 1).padStart(2, '0')}`,
  store_name: `Store ${index + 1}`,
  city: 'DELHI', region: 'NORTH',
  is_online_store: false, status: 'offline',
}));

async function sidebarGeometry(page) {
  return page.evaluate(() => {
    const aside = document.querySelector('aside');
    const box = aside.getBoundingClientRect();
    return {
      top: box.top, left: box.left, width: box.width,
      position: getComputedStyle(aside).position,
      documentScroll: document.documentElement.scrollTop,
      horizontalOverflow:
        document.documentElement.scrollWidth > window.innerWidth + 1,
    };
  });
}

/** Scroll everything that could scroll, not just the one we expect to. */
async function scrollFarDown(page) {
  await page.evaluate(() => {
    window.scrollBy(0, 8000);
    const main = document.querySelector('main');
    if (main) main.scrollTop = 8000;
  });
  await page.waitForTimeout(300);
}

test.beforeEach(async ({ page }) => {
  await signIn(page);
  await mockBackend(page, { stores: FLEET });
});

for (const [width, height] of [[1920, 1080], [1440, 900], [1280, 720], [1024, 768]]) {
  test(`the navigation stays put at ${width}x${height}`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await page.goto('/console');
    await expect(page.getByTestId('nav-history')).toBeVisible();

    const before = await sidebarGeometry(page);
    await scrollFarDown(page);
    const after = await sidebarGeometry(page);

    // The property an operator actually cares about.
    expect(Math.abs(after.top - before.top),
           `the navigation moved ${after.top - before.top}px up the viewport`)
      .toBeLessThanOrEqual(1);
    expect(after.left).toBe(before.left);
    expect(after.horizontalOverflow).toBe(false);

    // And it is still usable, not merely present.
    await expect(page.getByTestId('nav-active-broadcasts')).toBeVisible();
    await page.getByTestId('nav-active-broadcasts').click();
    await expect(page).toHaveURL(/\/active-broadcasts/);
    await expect(page.getByTestId('nav-history')).toBeVisible();
  });
}

test('the main region is what scrolls, not the document', async ({ page }) => {
  // A short viewport and the largest page size, because the Store picker now
  // paginates - the Console is no longer automatically taller than a screen.
  await page.setViewportSize({ width: 1280, height: 600 });
  await page.goto('/console');
  await page.getByTestId('stores-page-size').selectOption('50');
  await scrollFarDown(page);

  const state = await page.evaluate(() => ({
    documentScroll: document.documentElement.scrollTop,
    mainScroll: document.querySelector('main').scrollTop,
  }));
  // If the DOCUMENT scrolled, a sticky aside would eventually leave with it.
  expect(state.documentScroll).toBe(0);
  expect(state.mainScroll).toBeGreaterThan(0);
});

test('log out stays reachable from the bottom of a long page', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 700 });
  await page.goto('/console');
  await scrollFarDown(page);

  const logout = page.getByTestId('logout-btn');
  await expect(logout).toBeVisible();
  const box = await logout.boundingBox();
  // Inside the viewport, not merely in the DOM.
  expect(box.y).toBeGreaterThanOrEqual(0);
  expect(box.y + box.height).toBeLessThanOrEqual(700 + 1);
});

test('the navigation and the global recording player coexist', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 800 });
  await page.goto('/history');

  // The footer player is fixed at the bottom and offset past the sidebar.
  const play = page.getByTestId('recording-play-8');
  if (await play.count() === 0) {
    test.skip(true, 'this fixture has no recorded session to play');
  }
  await play.click();
  const bar = page.getByTestId('recording-player-bar');
  if (await bar.count()) {
    await scrollFarDown(page);
    const geometry = await page.evaluate(() => {
      const aside = document.querySelector('aside').getBoundingClientRect();
      const player = document.querySelector('[data-testid="recording-player-bar"]')
        .getBoundingClientRect();
      return { asideRight: aside.right, playerLeft: player.left, asideTop: aside.top };
    });
    // Neither hides the other: the player starts where the navigation ends.
    expect(geometry.playerLeft).toBeGreaterThanOrEqual(geometry.asideRight - 1);
    expect(Math.abs(geometry.asideTop)).toBeLessThanOrEqual(1);
    await expect(page.getByTestId('nav-console')).toBeVisible();
  }
});

test('a narrow viewport keeps its off-canvas navigation', async ({ page }) => {
  // Below the md breakpoint the sidebar is deliberately off-canvas behind a
  // toggle. A desktop-width sidebar must not be forced onto a phone.
  await page.setViewportSize({ width: 500, height: 800 });
  await page.goto('/console');
  await page.waitForSelector('aside', { state: 'attached' });

  const geometry = await sidebarGeometry(page);
  expect(geometry.position).toBe('fixed');
  // The page itself must not gain a horizontal scrollbar because of it.
  expect(geometry.horizontalOverflow).toBe(false);
});

// ===========================================================================
// The LIVE Console, which is where it was reported
// ===========================================================================
// Manual acceptance narrowed the symptom to "after the Broadcast goes live".
// A non-live Console is therefore not sufficient evidence, so these drive the
// live state: the on-air section, the three-card row, the Web Audience panel
// and a full Store table.

const LIVE_SESSION = {
  live: true,
  session: { id: 77, campaign_name: 'Evening announcement', target_mode: 'selected',
             started_at: '2026-08-07T09:00:00+00:00', status: 'live',
             selected_store_count: 40, online_store_count: 40, offline_store_count: 0 },
  targets: [],
};

const LIVE_ROOM = {
  public_code: 'EC-AAA111', status: 'OPEN', auto_approve: false, delivery: 'ok',
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

async function openLiveConsole(page) {
  await mockBackend(page, { stores: FLEET, current: LIVE_SESSION });
  await page.route('**/api/broadcast/sessions/*/web-room', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json',
                    body: JSON.stringify(LIVE_ROOM) }));
  await page.goto('/console');
  await expect(page.getByTestId('web-audience-panel')).toBeVisible();
}

test('CASE 1: the navigation stays put on a Console that is not live',
     async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 800 });
  await page.goto('/console');
  await expect(page.getByTestId('stores-search')).toBeVisible();

  const before = await sidebarGeometry(page);
  await scrollFarDown(page);
  const after = await sidebarGeometry(page);
  expect(Math.abs(after.top - before.top)).toBeLessThanOrEqual(1);
});

test('CASE 2: the navigation stays put while the Broadcast is LIVE',
     async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 800 });
  await openLiveConsole(page);

  const before = await sidebarGeometry(page);
  await scrollFarDown(page);
  const after = await sidebarGeometry(page);

  console.log('LIVE SIDEBAR:', JSON.stringify({
    beforeTop: +before.top.toFixed(1), afterTop: +after.top.toFixed(1),
    documentScroll: after.documentScroll }));

  expect(Math.abs(after.top - before.top),
         `the navigation moved ${(after.top - before.top).toFixed(1)}px while live`)
    .toBeLessThanOrEqual(1);
  // The document must not be the scroller, or a sticky aside eventually leaves.
  expect(after.documentScroll).toBe(0);
  expect(after.horizontalOverflow).toBe(false);

  // The TOP menu entries are what disappear first when this goes wrong.
  await expect(page.getByTestId('nav-console')).toBeVisible();
  await expect(page.getByTestId('nav-active-broadcasts')).toBeVisible();
  await page.getByTestId('nav-active-broadcasts').click();
  await expect(page).toHaveURL(/\/active-broadcasts/);
});

test('CASE 2b: log out stays reachable while the Broadcast is LIVE',
     async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 800 });
  await openLiveConsole(page);
  await scrollFarDown(page);

  const logout = page.getByTestId('logout-btn');
  await expect(logout).toBeVisible();
  const box = await logout.boundingBox();
  expect(box.y + box.height).toBeLessThanOrEqual(800 + 1);
});
