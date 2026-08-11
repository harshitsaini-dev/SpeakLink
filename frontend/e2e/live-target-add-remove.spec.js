/**
 * Adding and removing ONE Store while the broadcast is on air, in a real
 * browser, driven the way an operator drives it.
 *
 * The jest tests already prove the wiring. What only a browser can prove is
 * the thing an operator actually experiences: that pressing Remove on one row
 * changes that row and leaves the other alone, that the counts move with it,
 * and that a refusal appears beside the shop that refused rather than as a
 * page-wide banner over a console still claiming everything is fine.
 *
 * The backend is mocked. Whether a real Receiver joins at the live edge is
 * proved against real FFmpeg in the backend suite; that is a different
 * question from whether this console tells the truth about it.
 */
const { test, expect } = require('@playwright/test');
const { mockBackend, signIn, stubWebSocket } = require('./support/backend');

const AAA = { id: 101, store_code: 'AAA', store_name: 'North Shop',
              city: 'DELHI', region: 'NORTH', is_online_store: false,
              is_active: true, lifecycle_state: 'active', status: 'online' };
const BBB = { id: 102, store_code: 'BBB', store_name: 'South Shop',
              city: 'DELHI', region: 'NORTH', is_online_store: false,
              is_active: true, lifecycle_state: 'active', status: 'online' };
const DARK = { id: 103, store_code: 'CCC', store_name: 'Dark Shop',
               city: 'DELHI', region: 'NORTH', is_online_store: false,
               is_active: true, lifecycle_state: 'active', status: 'offline' };

const FLEET = [AAA, BBB, DARK];

async function serveInventory(page, stores) {
  await page.route('**/api/broadcast/target-stores', (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({
      stores,
      regions: Array.from(new Set(stores.map((s) => s.region))).sort(),
      cities: Array.from(new Set(stores.map((s) => s.city))).sort(),
    }),
  }));
}

/** A live broadcast reaching the named Stores, exactly as the server reports it. */
function liveOn(storeIds) {
  return {
    live: true,
    session: {
      id: 8, campaign_name: 'Evening announcement', status: 'live',
      target_mode: 'selected', started_at: new Date(0).toISOString(),
    },
    targets: storeIds.map((storeId, index) => ({
      id: index + 1, store_id: storeId, play_status: 'audio_receiving',
      lifecycle_state: 'ACTIVE', current_generation: 1,
    })),
    online_receivers: [AAA.id, BBB.id],
    ready_receivers: storeIds,
  };
}

test.beforeEach(async ({ page }) => {
  await signIn(page);
});


test('FLOW A: one Store is removed and the other keeps going', async ({ page }) => {
  await mockBackend(page, { stores: FLEET, current: liveOn([AAA.id, BBB.id]) });
  await serveInventory(page, FLEET);
  await stubWebSocket(page);
  await page.goto('/console');

  await expect(page.getByTestId('live-indicator')).toBeVisible();
  await expect(page.getByTestId('stat-selected')).toContainText('2');

  await page.getByTestId('remove-store-BBB').click();

  // The removed row flips to offering Add; the other row is untouched.
  await expect(page.getByTestId('add-store-BBB')).toBeVisible();
  await expect(page.getByTestId('remove-store-BBB')).toHaveCount(0);
  await expect(page.getByTestId('remove-store-AAA')).toBeVisible();

  // And the count follows participation, not the stale play_status the
  // removed row still carries.
  await expect(page.getByTestId('stat-selected')).toContainText('1');
});


test('FLOW B: a Store is added mid-broadcast and starts being counted',
     async ({ page }) => {
  await mockBackend(page, { stores: FLEET, current: liveOn([AAA.id]) });
  await serveInventory(page, FLEET);
  await stubWebSocket(page);
  await page.goto('/console');

  await expect(page.getByTestId('stat-selected')).toContainText('1');
  await expect(page.getByTestId('add-store-BBB')).toBeVisible();

  await page.getByTestId('add-store-BBB').click();

  await expect(page.getByTestId('remove-store-BBB')).toBeVisible();
  await expect(page.getByTestId('stat-selected')).toContainText('2');
});


test('FLOW C: remove then add back, which is how a shop is swapped mid-air',
     async ({ page }) => {
  await mockBackend(page, { stores: FLEET, current: liveOn([AAA.id, BBB.id]) });
  await serveInventory(page, FLEET);
  await stubWebSocket(page);
  await page.goto('/console');

  await page.getByTestId('remove-store-BBB').click();
  await expect(page.getByTestId('add-store-BBB')).toBeVisible();

  await page.getByTestId('add-store-BBB').click();
  await expect(page.getByTestId('remove-store-BBB')).toBeVisible();
  await expect(page.getByTestId('stat-selected')).toContainText('2');
});


test('FLOW D: a refusal appears on the row that refused, and only there',
     async ({ page }) => {
  await mockBackend(page, {
    stores: FLEET, current: liveOn([AAA.id]),
    addTargetRefusal: 'BBB did not report ready in time. It was not added and nothing else changed.',
  });
  await serveInventory(page, FLEET);
  await stubWebSocket(page);
  await page.goto('/console');

  await page.getByTestId('add-store-BBB').click();

  await expect(page.getByTestId('target-error-BBB'))
    .toContainText('did not report ready');
  // The other row carries no error, and the broadcast is still live. A shop
  // failing to join is not a broadcast failing.
  await expect(page.getByTestId('target-error-AAA')).toHaveCount(0);
  await expect(page.getByTestId('live-indicator')).toBeVisible();
  await expect(page.getByTestId('stat-selected')).toContainText('1');
});


test('FLOW E: an unreachable Store is never offered an Add button',
     async ({ page }) => {
  await mockBackend(page, { stores: FLEET, current: liveOn([AAA.id]) });
  await serveInventory(page, FLEET);
  await stubWebSocket(page);
  await page.goto('/console');

  // Offline Receiver: no button, and a reason in its place.
  await expect(page.getByTestId('add-store-CCC')).toHaveCount(0);
  await expect(page.getByTestId('add-blocked-CCC')).toContainText(/offline/i);
});


test('FLOW F: no Add or Remove control exists when nothing is on air',
     async ({ page }) => {
  await mockBackend(page, { stores: FLEET });
  await serveInventory(page, FLEET);
  await page.goto('/console');

  await expect(page.getByTestId('store-row-AAA')).toBeVisible();
  await expect(page.getByTestId('add-store-AAA')).toHaveCount(0);
  await expect(page.getByTestId('remove-store-AAA')).toHaveCount(0);
});
