/**
 * Where the live Broadcast Console puts its three cards.
 *
 * The Web Audience used to sit below the Store table. During a live broadcast
 * that meant scrolling past forty Stores to see who was listening, or to admit
 * somebody waiting - so it now sits between the controls and the targets, where
 * an operator is already looking.
 *
 * These assertions are geometric. "The element exists" would pass with the card
 * back at the bottom of the page, which is the exact thing being fixed.
 */
const { test, expect } = require('@playwright/test');
const { mockBackend, signIn } = require('./support/backend');

const FLEET = Array.from({ length: 25 }, (_, index) => ({
  id: index + 1,
  store_code: `S${String(index + 1).padStart(2, '0')}`,
  store_name: `Store ${index + 1}`,
  city: 'DELHI', region: 'NORTH',
  is_online_store: false, status: index % 3 === 0 ? 'online' : 'offline',
}));

/** A live session, so the audience card renders its real panel. */
const LIVE = {
  live: true,
  session: { id: 77, campaign_name: 'Evening announcement', target_mode: 'selected',
             started_at: '2026-08-07T09:00:00+00:00', status: 'live',
             selected_store_count: 1, online_store_count: 1, offline_store_count: 0 },
  targets: [],
};

const ROOM = {
  public_code: 'EC-K7Q92A', status: 'OPEN', auto_approve: false, delivery: 'ok',
  password: 'Q7KM-92PX', password_configured: true, password_rotated_at: null,
  counts: { waiting: 2, admitted: 3, connected: 3, listening: 2, buffering: 1, paused: 0 },
  waiting: Array.from({ length: 12 }, (_, i) => ({
    id: 500 + i, display_name: `Waiting ${i + 1}`,
    admission_status: 'REQUESTED', requested_at: '10:32:12' })),
  listeners: Array.from({ length: 30 }, (_, i) => ({
    id: 600 + i, display_name: `Listener ${i + 1}`, admitted_by: 'password',
    admission_status: 'PASSWORD_ADMITTED', playback_state: 'LISTENING',
    connected: true, seconds_since_seen: 1, stale: false })),
};

async function openLiveConsole(page) {
  await signIn(page);
  await mockBackend(page, { stores: FLEET, current: LIVE });
  await page.route('**/api/broadcast/sessions/*/web-room', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json',
                    body: JSON.stringify(ROOM) }));
  await page.goto('/console');
}

async function boxes(page) {
  return page.evaluate(() => {
    const read = (id) => {
      const node = document.querySelector(`[data-testid="${id}"]`);
      if (!node) return null;
      const box = node.getBoundingClientRect();
      return { left: box.left, right: box.right, top: box.top,
               bottom: box.bottom, width: box.width, height: box.height };
    };
    return {
      controls: read('console-controls-card'),
      audience: read('console-audience-card'),
      targets: read('target-summary'),
      stores: read('stores-search'),
      horizontalOverflow:
        document.documentElement.scrollWidth > window.innerWidth + 1,
    };
  });
}

test('desktop puts Controls, Web Audience and Targets on one row', async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await openLiveConsole(page);
  await expect(page.getByTestId('web-audience-panel')).toBeVisible();

  const layout = await boxes(page);
  expect(layout.controls).not.toBeNull();
  expect(layout.audience).not.toBeNull();
  expect(layout.targets).not.toBeNull();

  // Left to right, in the order the operator asked for.
  expect(layout.controls.left).toBeLessThan(layout.audience.left);
  expect(layout.audience.left).toBeLessThan(layout.targets.left);

  // One row: their tops line up.
  expect(Math.abs(layout.audience.top - layout.controls.top)).toBeLessThanOrEqual(4);
  expect(Math.abs(layout.targets.top - layout.controls.top)).toBeLessThanOrEqual(4);

  // The middle card is the widest, and the outer two are not cramped.
  expect(layout.audience.width).toBeGreaterThan(layout.controls.width);
  expect(layout.audience.width).toBeGreaterThan(layout.targets.width);
  expect(layout.controls.width).toBeGreaterThan(layout.targets.width);
  expect(layout.horizontalOverflow).toBe(false);
});

test('the Store table begins below that row', async ({ page }) => {
  // Asserted on a Console that is NOT live, because that is where the Store
  // picker is definitely rendered - a live session may legitimately present
  // its targets differently, and the ordering claim is about the page.
  await page.setViewportSize({ width: 1600, height: 900 });
  await signIn(page);
  await mockBackend(page, { stores: FLEET });
  await page.goto('/console');
  // Wait for the picker rather than measuring a page still assembling itself.
  await expect(page.getByTestId('stores-search')).toBeVisible();

  const layout = await boxes(page);
  expect(layout.stores).not.toBeNull();
  // The whole point: the audience is ABOVE the Stores, not buried under them.
  expect(layout.stores.top).toBeGreaterThan(layout.audience.top);
  expect(layout.stores.top).toBeGreaterThan(layout.targets.top);
});

test('the audience is reachable without scrolling past the Stores', async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await openLiveConsole(page);

  // Visible in the first screen, which is what "immediately visible" means.
  await expect(page.getByTestId('web-room-code')).toBeInViewport();
  await expect(page.getByTestId('web-count-listening')).toBeInViewport();
});

test('a large audience does not stretch the row', async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await openLiveConsole(page);
  await expect(page.getByTestId('web-audience-panel')).toBeVisible();

  const layout = await boxes(page);
  // Thirty listeners and twelve waiting, in internally scrolling lists: the
  // row stays a row rather than becoming a page.
  expect(layout.audience.height,
         `the audience card is ${Math.round(layout.audience.height)}px tall`)
    .toBeLessThan(900);

  // And the actions are still there to be used.
  await expect(page.getByTestId('web-kick-600')).toBeAttached();
  await expect(page.getByTestId('web-approve-500')).toBeAttached();
});

test('the cards stack on a narrow screen without horizontal overflow',
     async ({ page }) => {
  await page.setViewportSize({ width: 800, height: 900 });
  await openLiveConsole(page);
  await expect(page.getByTestId('web-audience-panel')).toBeVisible();

  const layout = await boxes(page);
  // Stacked: each begins below the previous rather than beside it.
  expect(layout.audience.top).toBeGreaterThan(layout.controls.top);
  expect(layout.targets.top).toBeGreaterThan(layout.audience.top);
  expect(layout.horizontalOverflow).toBe(false);

  // Controls, Web Audience, Targets - and the Stores below them all.
  if (layout.stores) expect(layout.stores.top).toBeGreaterThan(layout.targets.top);
});

test('a Broadcast that is not live explains the link instead of pretending',
     async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await signIn(page);
  await mockBackend(page, { stores: FLEET });
  await page.goto('/console');

  const card = page.getByTestId('console-audience-card');
  await expect(card).toBeVisible();
  await expect(card).toContainText(/listener link is created when this Broadcast starts/i);
  // No invented room code before a room exists.
  await expect(page.getByTestId('web-room-code')).toHaveCount(0);
});
