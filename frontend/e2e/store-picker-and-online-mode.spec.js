/**
 * The Store picker and Online Stores Only, in a real browser.
 *
 * The defect these exist for: Online Stores Only filtered on the Online /
 * Physical business flag rather than Receiver connectivity, so a console
 * showing BP ONLINE resolved zero targets.
 *
 * The picker half is about a fleet that no longer fits on one screen - and
 * specifically about the two things that break first: a selection surviving a
 * page change, and a Zone FILTER quietly becoming Zone TARGETING.
 */
const { test, expect } = require('@playwright/test');
const { mockBackend, signIn } = require('./support/backend');

const ZONES = ['NORTH', 'SOUTH', 'WEST'];
const CITIES = ['DELHI', 'MUMBAI', 'JAIPUR'];

/** 25 Stores, so the default page size of 10 gives three pages. */
function fleet() {
  return Array.from({ length: 25 }, (_, index) => ({
    id: index + 1,
    store_code: `S${String(index + 1).padStart(2, '0')}`,
    store_name: `Store ${index + 1}`,
    city: CITIES[index % CITIES.length],
    region: ZONES[index % ZONES.length],
    is_online_store: false,
    status: index % 3 === 0 ? 'online' : 'offline',
  }));
}

const BP = { id: 101, store_code: 'BP', store_name: 'Bindapur', city: 'DELHI',
             region: 'UN ZONE', is_online_store: false, status: 'online' };
const RG = { id: 102, store_code: 'RG', store_name: 'Rohini Gardens',
             city: 'DELHI', region: 'UN ZONE', is_online_store: false,
             status: 'offline' };
//: Flagged "Online" in Store Management, but no Receiver. This is the Store the
//: old implementation would have targeted.
const WEB = { id: 103, store_code: 'WEB', store_name: 'Web Store', city: 'DELHI',
              region: 'UN ZONE', is_online_store: true, status: 'offline' };

/** Serve the target inventory, and let a test change it mid-flight. */
async function serveInventory(page, initial) {
  const state = { stores: initial };
  await page.route('**/api/broadcast/target-stores', (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({
      stores: state.stores,
      regions: Array.from(new Set(state.stores.map((s) => s.region))).sort(),
      cities: Array.from(new Set(state.stores.map((s) => s.city))).sort(),
    }),
  }));
  return state;
}

async function visibleCodes(page) {
  return page.evaluate(() => Array.from(
    document.querySelectorAll('[data-testid^="store-row-"]'))
    .map((row) => row.getAttribute('data-testid').replace('store-row-', '')));
}

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

// ---- FLOW A: online mode resolves connectivity -------------------------
test('FLOW A: Online Stores Only targets the connected Store', async ({ page }) => {
  await mockBackend(page, { stores: [BP, RG, WEB] });
  await serveInventory(page, [BP, RG, WEB]);
  await page.goto('/console');

  await page.getByTestId('target-mode-select').selectOption('online_only');

  // BP is a PHYSICAL shop with a connected Receiver. WEB is flagged Online in
  // Store Management and has none.
  await expect(page.getByTestId('stat-selected')).toContainText('1');
  await expect(page.getByTestId('stat-selected')).toContainText(/targets/i);
  await expect(page.getByTestId('stat-online')).toContainText('1');
  await expect(page.getByTestId('stat-offline')).toContainText('2');

  // And the request the operator would send names that mode, not a Store list.
  await page.getByTestId('campaign-name-input').fill('Evening announcement');
  await expect(page.getByTestId('start-broadcast-btn')).toBeEnabled();
});

test('FLOW A2: nothing online cannot be started', async ({ page }) => {
  await mockBackend(page, { stores: [RG, WEB] });
  await serveInventory(page, [RG, WEB]);
  await page.goto('/console');

  await page.getByTestId('target-mode-select').selectOption('online_only');
  await page.getByTestId('campaign-name-input').fill('Nobody is listening');
  await expect(page.getByTestId('stat-selected')).toContainText('0');
  await expect(page.getByTestId('start-broadcast-btn')).toBeDisabled();
});

// ---- FLOW D: refresh reflects a status change --------------------------
test('FLOW D: a Store coming online changes the preview after a refresh',
     async ({ page }) => {
  await mockBackend(page, { stores: [BP, RG] });
  const state = await serveInventory(page, [BP, RG]);
  await page.goto('/console');
  await page.getByTestId('target-mode-select').selectOption('online_only');
  await expect(page.getByTestId('stat-selected')).toContainText('1');

  // RG's Receiver reconnects. The Console polls the inventory.
  state.stores = [BP, { ...RG, status: 'online' }];
  await expect(page.getByTestId('stat-selected')).toContainText('2', { timeout: 10_000 });
  await expect(page.getByTestId('stat-offline')).toContainText('0');
});

// ---- FLOW B: pagination and selection ----------------------------------
test('FLOW B: a selection survives moving between pages', async ({ page }) => {
  await mockBackend(page, { stores: fleet() });
  await serveInventory(page, fleet());
  await page.goto('/console');

  await expect(page.getByTestId('stores-page-info')).toContainText('Page 1 of 3');
  expect((await visibleCodes(page)).length).toBe(10);

  await page.getByTestId('store-checkbox-S01').check();
  await page.getByTestId('stores-next-page').click();
  await expect(page.getByTestId('stores-page-info')).toContainText('Page 2 of 3');
  await page.getByTestId('store-checkbox-S11').check();
  await expect(page.getByTestId('stat-selected')).toContainText('2');

  await page.getByTestId('stores-prev-page').click();
  // The target set belongs to the broadcast, not to the visible page.
  await expect(page.getByTestId('store-checkbox-S01')).toBeChecked();
  await expect(page.getByTestId('stat-selected')).toContainText('2');
});

// ---- FLOW C: filtering keeps the selection -----------------------------
test('FLOW C: a selected Store hidden by a filter is still selected',
     async ({ page }) => {
  await mockBackend(page, { stores: fleet() });
  await serveInventory(page, fleet());
  await page.goto('/console');

  await page.getByTestId('store-checkbox-S01').check();
  await page.getByTestId('stores-search').fill('Store 2');
  await expect(page.getByTestId('store-checkbox-S01')).toHaveCount(0);
  await expect(page.getByTestId('stores-selected-count')).toContainText('1');

  await page.getByTestId('stores-clear-filters').click();
  await expect(page.getByTestId('store-checkbox-S01')).toBeChecked();
  await expect(page.getByTestId('stat-selected')).toContainText('1');
});

test('FLOW C2: the Zone filter narrows rows without changing targeting',
     async ({ page }) => {
  await mockBackend(page, { stores: fleet() });
  await serveInventory(page, fleet());
  await page.goto('/console');

  await page.getByTestId('store-checkbox-S01').check();
  await page.getByTestId('stores-filter-zone').selectOption('NORTH');

  // Filtering is a view, not a target decision.
  await expect(page.getByTestId('target-mode-select')).toHaveValue('selected');
  await expect(page.getByTestId('stat-selected')).toContainText('1');
  await expect(page.getByTestId('stores-page-info')).toContainText('Page 1');
});

test('the status filter separates connected from merely flagged online',
     async ({ page }) => {
  await mockBackend(page, { stores: [BP, RG, WEB] });
  await serveInventory(page, [BP, RG, WEB]);
  await page.goto('/console');

  await page.getByTestId('stores-filter-status').selectOption('online');
  expect(await visibleCodes(page)).toEqual(['BP']);

  await page.getByTestId('stores-filter-status').selectOption('offline');
  expect((await visibleCodes(page)).sort()).toEqual(['RG', 'WEB']);
});

test('Select page takes the visible rows only', async ({ page }) => {
  await mockBackend(page, { stores: fleet() });
  await serveInventory(page, fleet());
  await page.goto('/console');

  await page.getByTestId('select-page-btn').click();
  await expect(page.getByTestId('stat-selected')).toContainText('10');

  await page.getByTestId('select-all-filtered-btn').click();
  await expect(page.getByTestId('stat-selected')).toContainText('25');

  await page.getByTestId('clear-selection-btn').click();
  await expect(page.getByTestId('stat-selected')).toContainText('0');
});

// ---- FLOW E / F: permission and Link-only ------------------------------
test('FLOW E: without physical delivery the inventory is never requested',
     async ({ page }) => {
  const asked = [];
  page.on('request', (request) => {
    if (request.url().includes('/broadcast/target-stores')) asked.push(request.url());
  });
  await mockBackend(page, {
    stores: fleet(),
    permissions: ['menu.broadcast.view', 'broadcast.start', 'broadcast.stop',
                  'menu.history.view'],
  });
  await page.goto('/console');
  await expect(page.getByTestId('link-only-mode')).toBeVisible();

  await page.waitForTimeout(1500);
  expect(asked, 'the physical inventory was requested anyway').toEqual([]);
  await expect(page.getByTestId('stores-search')).toHaveCount(0);
  await expect(page.getByTestId('stores-page-info')).toHaveCount(0);
});

test('FLOW F: Only With Link renders no Store picker', async ({ page }) => {
  await mockBackend(page, { stores: fleet() });
  await serveInventory(page, fleet());
  await page.goto('/console');

  await page.getByTestId('target-mode-select').selectOption('only_with_link');
  await expect(page.getByTestId('stores-search')).toHaveCount(0);
  await expect(page.getByTestId('stores-page-info')).toHaveCount(0);
  await expect(page.getByTestId('stat-selected')).toContainText('0');

  // A link-only broadcast is startable with no Stores at all.
  await page.getByTestId('campaign-name-input').fill('Web only');
  await expect(page.getByTestId('start-broadcast-btn')).toBeEnabled();
});
