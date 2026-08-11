/**
 * The two defects found in review, driven in a real browser.
 *
 * 1. A permanently deleted Receiver Device stayed on screen. It must be gone
 *    from the operational list - and gone from the RESPONSE, not merely
 *    hidden with CSS, which is why these assert on rendered rows after a real
 *    request rather than on visibility.
 *
 * 2. The lifecycle filter offered two options that meant the same thing, and
 *    selecting one lifecycle left the previous one still showing.
 */
const { test, expect } = require('@playwright/test');
const { mockBackend, signIn } = require('./support/backend');

test.beforeEach(async ({ page }) => { await signIn(page); });

const CATALOG = [
  { id: 1, store_code: 'ACT', store_name: 'Active Store', city: 'DELHI', region: 'NORTH',
    is_active: true, lifecycle_state: 'active', status: 'online' },
  { id: 2, store_code: 'DIS', store_name: 'Disabled Store', city: 'DELHI', region: 'NORTH',
    is_active: false, lifecycle_state: 'disabled', status: 'offline' },
  { id: 3, store_code: 'ARC', store_name: 'Archived Store', city: 'MUMBAI', region: 'WEST',
    is_active: false, lifecycle_state: 'archived', status: 'offline' },
  { id: 4, store_code: 'GONE', store_name: 'Tombstoned Store', city: 'DELHI', region: 'NORTH',
    is_active: false, lifecycle_state: 'deleted', status: 'offline' },
];

// One Device of each kind, in the same Store.
const DEVICES = [
  { public_id: 'aaaaaaaa-0000-0000-0000-000000000001', display_name: 'Active till',
    status: 'active', role: 'PRIMARY', enrolled_at: '2026-07-27T09:12:00+00:00',
    store_id: 1 },
  { public_id: 'bbbbbbbb-0000-0000-0000-000000000002', display_name: 'Archived till',
    status: 'disabled', role: 'STANDBY', enrolled_at: '2026-07-27T09:20:00+00:00',
    archived_at: '2026-08-01T12:00:00+00:00', store_id: 1 },
  { public_id: 'cccccccc-0000-0000-0000-000000000003', display_name: 'Deleted till',
    status: 'retired', role: 'STANDBY', enrolled_at: '2026-07-27T09:30:00+00:00',
    deleted_at: '2026-08-02T00:00:00+00:00', store_id: 1 },
];

// ===========================================================================
// Store Management lifecycle
// ===========================================================================
test('the default view is Active only', async ({ page }) => {
  await mockBackend(page, { stores: CATALOG });
  await page.goto('/stores');
  await expect(page.getByTestId('result-count')).toContainText('1 result');
  await expect(page.getByTestId('edit-store-ACT')).toBeVisible();
  await expect(page.getByTestId('edit-store-DIS')).toHaveCount(0);
  await expect(page.getByTestId('edit-store-ARC')).toHaveCount(0);
});

test('the lifecycle control offers no duplicate and no deleted option', async ({ page }) => {
  await mockBackend(page, { stores: CATALOG });
  await page.goto('/stores');
  await expect(page.getByTestId('stores-lifecycle')).toBeVisible();
  const labels = await page.getByTestId('stores-lifecycle').locator('option')
    .allTextContents();
  expect(labels).toEqual(['All Current', 'Active', 'Disabled', 'Archived']);
  expect(labels.filter((l) => /delete/i.test(l))).toHaveLength(0);
  expect(new Set(labels).size).toBe(labels.length);
});

test('Disabled and Archived each return only their own state', async ({ page }) => {
  await mockBackend(page, { stores: CATALOG });
  await page.goto('/stores');

  await page.getByTestId('stores-lifecycle').selectOption('disabled');
  await expect(page.getByTestId('edit-store-DIS')).toBeVisible();
  await expect(page.getByTestId('edit-store-ACT')).toHaveCount(0);

  await page.getByTestId('stores-lifecycle').selectOption('archived');
  // An archived Store offers Restore rather than Edit, so its lifecycle badge
  // is what identifies the row.
  await expect(page.getByTestId('lifecycle-ARCHIVED')).toBeVisible();
  await expect(page.getByTestId('edit-store-DIS')).toHaveCount(0);
});

test('selecting Archived then Active shows ONLY Active', async ({ page }) => {
  // The reported symptom, exactly.
  await mockBackend(page, { stores: CATALOG });
  await page.goto('/stores');

  await page.getByTestId('stores-lifecycle').selectOption('archived');
  await expect(page.getByTestId('lifecycle-ARCHIVED')).toBeVisible();

  await page.getByTestId('stores-lifecycle').selectOption('active');
  await expect(page.getByTestId('edit-store-ACT')).toBeVisible();
  await expect(page.getByTestId('lifecycle-ARCHIVED')).toHaveCount(0);
  await expect(page.getByTestId('result-count')).toContainText('1 result');
});

test('All Current shows every state except deleted', async ({ page }) => {
  await mockBackend(page, { stores: CATALOG });
  await page.goto('/stores');
  await page.getByTestId('stores-lifecycle').selectOption('all_current');
  await expect(page.getByTestId('result-count')).toContainText('3 results');
  await expect(page.getByTestId('edit-store-GONE')).toHaveCount(0);
});

test('a permanently deleted Store is absent from every selection', async ({ page }) => {
  await mockBackend(page, { stores: CATALOG });
  await page.goto('/stores');
  for (const selection of ['all_current', 'active', 'disabled', 'archived']) {
    await page.getByTestId('stores-lifecycle').selectOption(selection);
    await expect(page.getByTestId('edit-store-GONE')).toHaveCount(0);
  }
});

test('search, Zone, City and lifecycle combine', async ({ page }) => {
  await mockBackend(page, { stores: CATALOG });
  await page.goto('/stores');
  await page.getByTestId('stores-lifecycle').selectOption('all_current');
  await page.getByTestId('stores-zone').selectOption('NORTH');
  await page.getByTestId('stores-city').selectOption('DELHI');
  await page.getByTestId('stores-search').fill('Disabled');
  await expect(page.getByTestId('result-count')).toContainText('1 result');
  await expect(page.getByTestId('edit-store-DIS')).toBeVisible();
});

test('Clear Filters returns to the default Active view', async ({ page }) => {
  await mockBackend(page, { stores: CATALOG });
  await page.goto('/stores');
  await page.getByTestId('stores-lifecycle').selectOption('all_current');
  await expect(page.getByTestId('result-count')).toContainText('3 results');
  await page.getByTestId('clear-filters').click();
  await expect(page.getByTestId('result-count')).toContainText('1 result');
  await expect(page.getByTestId('edit-store-ACT')).toBeVisible();
});

test('Create, Edit and Archive still work', async ({ page }) => {
  await mockBackend(page, { stores: CATALOG });
  await page.goto('/stores');
  await page.getByTestId('add-store-btn').click();
  await expect(page.getByTestId('add-store-modal')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('edit-store-ACT')).toBeVisible();
  await expect(page.getByTestId('archive-store-ACT')).toBeVisible();
});

// ===========================================================================
// Receiver Device visibility
// ===========================================================================
test('a permanently deleted Device is absent from the per-Store list', async ({ page }) => {
  await mockBackend(page, { stores: CATALOG, devices: DEVICES });
  await page.goto('/stores/1/devices');

  // Active is visible; archived is hidden by this page's own Show Archived
  // toggle, which is correct and unrelated.
  await expect(page.getByTestId(`device-row-${DEVICES[0].public_id}`)).toBeVisible();
  await expect(page.getByTestId(`device-row-${DEVICES[2].public_id}`)).toHaveCount(0);

  // The property that matters: turning Show Archived ON reveals the archived
  // Device and still never reveals the permanently deleted one.
  await page.getByTestId('show-archived-toggle').check();
  await expect(page.getByTestId(`device-row-${DEVICES[1].public_id}`)).toBeVisible();
  await expect(page.getByTestId(`device-row-${DEVICES[2].public_id}`)).toHaveCount(0);
});

test('the deleted Device is absent from the RESPONSE, not merely hidden', async ({ page }) => {
  await mockBackend(page, { stores: CATALOG, devices: DEVICES });
  const bodies = [];
  page.on('response', async (response) => {
    if (response.url().includes('/receiver-devices/roles')) {
      bodies.push(await response.text());
    }
  });
  await page.goto('/stores/1/devices');
  await expect(page.getByTestId(`device-row-${DEVICES[0].public_id}`)).toBeVisible();

  expect(bodies.length).toBeGreaterThan(0);
  for (const body of bodies) {
    expect(body).not.toContain(DEVICES[2].public_id);
  }
});

test('the fleet lifecycle control offers no duplicate and no deleted option',
     async ({ page }) => {
  await mockBackend(page, { stores: CATALOG, devices: DEVICES });
  await page.goto('/devices');
  await expect(page.getByTestId('fleet-lifecycle')).toBeVisible();
  const labels = await page.getByTestId('fleet-lifecycle').locator('option').allTextContents();
  expect(labels).toEqual(['All Current', 'Active', 'Archived']);
  expect(labels.filter((l) => /delete/i.test(l))).toHaveLength(0);
  expect(new Set(labels).size).toBe(labels.length);
});

test('the fleet never shows a permanently deleted Device, whatever is selected',
     async ({ page }) => {
  await mockBackend(page, { stores: CATALOG, devices: DEVICES });
  await page.goto('/devices');
  for (const selection of ['all_current', 'active', 'archived']) {
    await page.getByTestId('fleet-lifecycle').selectOption(selection);
    await expect(page.getByTestId(`fleet-row-${DEVICES[2].public_id}`)).toHaveCount(0);
  }
});

test('fleet: selecting Archived then All Current does not keep Archived pinned',
     async ({ page }) => {
  await mockBackend(page, { stores: CATALOG, devices: DEVICES });
  await page.goto('/devices');

  await page.getByTestId('fleet-lifecycle').selectOption('archived');
  await expect(page.getByTestId(`fleet-row-${DEVICES[1].public_id}`)).toBeVisible();
  await expect(page.getByTestId(`fleet-row-${DEVICES[0].public_id}`)).toHaveCount(0);

  await page.getByTestId('fleet-lifecycle').selectOption('active');
  await expect(page.getByTestId(`fleet-row-${DEVICES[0].public_id}`)).toBeVisible();
  await expect(page.getByTestId(`fleet-row-${DEVICES[1].public_id}`)).toHaveCount(0);
});
