/**
 * Store Management search and filter, in a real browser.
 *
 * The unit tests prove the query reaches the server. These prove an operator
 * can drive it, and - the part that matters most on a page carrying
 * destructive controls - that adding search did not disturb Create, Edit,
 * Archive or Delete.
 */
const { test, expect } = require('@playwright/test');
const { mockBackend, signIn } = require('./support/backend');

test.beforeEach(async ({ page }) => { await signIn(page); });

const CATALOG = [
  { id: 1, store_code: 'UN', store_name: 'Uttam Nagar Old', city: 'DELHI', region: 'NORTH',
    is_online_store: true, is_active: true, lifecycle_state: 'active', status: 'online' },
  { id: 2, store_code: 'ASR', store_name: 'Uttam Nagar ASR', city: 'DELHI', region: 'NORTH',
    is_online_store: false, is_active: true, lifecycle_state: 'active', status: 'offline' },
  { id: 5, store_code: 'AW', store_name: 'Andheri West', city: 'MUMBAI', region: 'WEST',
    is_online_store: false, is_active: true, lifecycle_state: 'active', status: 'offline' },
];

test('searching by Store name narrows the list and the count', async ({ page }) => {
  await mockBackend(page, { stores: CATALOG });
  await page.goto('/stores');
  await expect(page.getByTestId('result-count')).toContainText('3 results');

  await page.getByTestId('stores-search').fill('Andheri');
  await expect(page.getByTestId('result-count')).toContainText('1 result');
  await expect(page.getByTestId('edit-store-AW')).toBeVisible();
  await expect(page.getByTestId('edit-store-UN')).toHaveCount(0);
});

test('searching by Store code works too', async ({ page }) => {
  await mockBackend(page, { stores: CATALOG });
  await page.goto('/stores');
  await page.getByTestId('stores-search').fill('ASR');
  await expect(page.getByTestId('edit-store-ASR')).toBeVisible();
  await expect(page.getByTestId('edit-store-AW')).toHaveCount(0);
});

test('Zone and City filter, and combine with search', async ({ page }) => {
  await mockBackend(page, { stores: CATALOG });
  await page.goto('/stores');

  await page.getByTestId('stores-zone').selectOption('NORTH');
  await expect(page.getByTestId('result-count')).toContainText('2 results');

  await page.getByTestId('stores-city').selectOption('DELHI');
  await expect(page.getByTestId('result-count')).toContainText('2 results');

  await page.getByTestId('stores-search').fill('ASR');
  await expect(page.getByTestId('result-count')).toContainText('1 result');
  await expect(page.getByTestId('edit-store-ASR')).toBeVisible();
});

test('Clear Filters restores the whole list', async ({ page }) => {
  await mockBackend(page, { stores: CATALOG });
  await page.goto('/stores');
  await page.getByTestId('stores-zone').selectOption('WEST');
  await expect(page.getByTestId('result-count')).toContainText('1 result');
  await page.getByTestId('clear-filters').click();
  await expect(page.getByTestId('result-count')).toContainText('3 results');
});

test('a filter that matches nothing says so rather than looking broken', async ({ page }) => {
  await mockBackend(page, { stores: CATALOG });
  await page.goto('/stores');
  await page.getByTestId('stores-search').fill('no-such-store');
  await expect(page.getByTestId('list-empty')).toBeVisible();
  await expect(page.getByTestId('list-error')).toHaveCount(0);
});

test('a refused request reports permission rather than an empty catalog', async ({ page }) => {
  await mockBackend(page, { stores: CATALOG, storesListStatus: 403 });
  await page.goto('/stores');
  await expect(page.getByTestId('list-error')).toContainText(/permission/i);
  await expect(page.getByTestId('list-empty')).toHaveCount(0);
});

test('Zone options come from the server, not from the visible page', async ({ page }) => {
  await mockBackend(page, { stores: CATALOG });
  await page.goto('/stores');
  // Narrowed to one Zone, the other must still be offered - the dropdown is
  // built from the scoped catalog, not from what happens to be on screen.
  await page.getByTestId('stores-zone').selectOption('WEST');
  await expect(page.getByTestId('stores-zone')).toContainText('NORTH');
});

test('adding search did not disturb Create, Edit or Archive', async ({ page }) => {
  await mockBackend(page, { stores: CATALOG });
  await page.goto('/stores');

  await expect(page.getByTestId('add-store-btn')).toBeVisible();
  await page.getByTestId('add-store-btn').click();
  await expect(page.getByTestId('add-store-modal')).toBeVisible();
  await page.keyboard.press('Escape');

  await expect(page.getByTestId('edit-store-UN')).toBeVisible();
  await expect(page.getByTestId('archive-store-UN')).toBeVisible();
});

test('a permanently deleted Store never appears in the list', async ({ page }) => {
  await mockBackend(page, {
    stores: [...CATALOG, {
      id: 9, store_code: 'GONE', store_name: 'Tombstoned', city: 'DELHI',
      region: 'NORTH', is_active: false, lifecycle_state: 'deleted', status: 'offline',
    }],
  });
  await page.goto('/stores');
  await expect(page.getByTestId('edit-store-GONE')).toHaveCount(0);
  await expect(page.getByTestId('result-count')).toContainText('3 results');
});
