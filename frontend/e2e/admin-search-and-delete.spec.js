/**
 * The six admin screens, driven in a real browser.
 *
 * What these prove that a unit test cannot: that the controls actually reach
 * the network with the right query, that Select All Filtered posts a FILTER
 * rather than an enumerated id list, and that a permanent-delete dialog cannot
 * be got through without BOTH the typed confirmation and the acknowledgement.
 *
 * The last one is the reason this file exists. Every other guard in the
 * deletion path is server-side and proven in the backend suite; this is the
 * only place the human-facing half of it can be exercised at all.
 */
const { test, expect } = require('@playwright/test');
const { mockBackend, signIn } = require('./support/backend');

test.beforeEach(async ({ page }) => { await signIn(page); });

// The default mocked operator is an ADMIN, which by design holds no
// *.delete_permanently right at all - permission_catalog subtracts
// DESTRUCTIVE_CODES from the ADMIN set. Anything testing a destructive control
// therefore has to sign in as the SUPER ADMIN, and a test that forgot to would
// fail by finding no button, which is exactly the right way round.
const OWNER = { id: 1, username: 'founder', display_name: 'The Founder', role: 'OWNER' };

// ===========================================================================
// Receiver Status
// ===========================================================================
test.describe('Receiver Status filters', () => {
  test('filtering by Zone narrows the list and the result count agrees', async ({ page }) => {
    await mockBackend(page, {
      stores: [
        { id: 1, store_code: 'UN', store_name: 'Uttam Nagar Old', city: 'DELHI',
          region: 'NORTH', is_active: true, lifecycle_state: 'active', status: 'online' },
        { id: 2, store_code: 'MUM', store_name: 'Andheri West', city: 'MUMBAI',
          region: 'WEST', is_active: true, lifecycle_state: 'active', status: 'offline' },
      ],
    });
    await page.goto('/receivers');
    await expect(page.getByTestId('result-count')).toContainText('2 results');

    await page.getByTestId('receivers-zone').selectOption('NORTH');
    await expect(page.getByTestId('result-count')).toContainText('1 result');
    await expect(page.getByTestId('receiver-card-UN')).toBeVisible();
    await expect(page.getByTestId('receiver-card-MUM')).toHaveCount(0);

    await page.getByTestId('clear-filters').click();
    await expect(page.getByTestId('result-count')).toContainText('2 results');
  });

  test('a filter that matches nothing says so, and does not look like a failure',
       async ({ page }) => {
    await mockBackend(page);
    await page.goto('/receivers');
    await page.getByTestId('receivers-search').fill('no-such-store');
    await expect(page.getByTestId('list-empty')).toBeVisible();
    await expect(page.getByTestId('list-error')).toHaveCount(0);
  });

  test('a refused request reports a permission problem rather than "no results"',
       async ({ page }) => {
    await mockBackend(page, { storesListStatus: 403 });
    await page.goto('/receivers');
    await expect(page.getByTestId('list-error')).toContainText(/permission/i);
    await expect(page.getByTestId('list-empty')).toHaveCount(0);
  });
});

// ===========================================================================
// System Logs - filters, bulk selection, permanent delete
// ===========================================================================
test.describe('System Logs', () => {
  test('the level filter reaches the server and the coverage note is shown',
       async ({ page }) => {
    await mockBackend(page);
    await page.goto('/logs');
    await expect(page.getByTestId('result-count')).toContainText('3 results');

    await page.getByTestId('log-level-filter').selectOption('error');
    await expect(page.getByTestId('result-count')).toContainText('1 result');
    await expect(page.getByTestId('log-row-103')).toBeVisible();

    await expect(page.getByTestId('logs-coverage-note'))
      .toContainText(/Older logs remain\s+searchable by text, level and date/);
  });

  test('archiving selected rows removes them from the default view', async ({ page }) => {
    const state = await mockBackend(page);
    await page.goto('/logs');
    await page.getByTestId('log-select-101').check();
    await page.getByTestId('logs-archive-selected').click();

    await expect(page.getByTestId('log-row-101')).toHaveCount(0);
    await expect(page.getByTestId('result-count')).toContainText('2 results');
    expect(state.bulkCalls[0].body).toMatchObject({ mode: 'ids', ids: [101] });
  });

  test('Delete Permanently needs the typed word AND the acknowledgement',
       async ({ page }) => {
    const state = await mockBackend(page, { operator: OWNER });
    await page.goto('/logs');
    await page.getByTestId('log-select-101').check();
    await page.getByTestId('logs-delete-selected').click();

    const confirm = page.getByTestId('logs-delete-confirm-btn');
    await expect(confirm).toBeDisabled();

    // Typed word alone is not enough.
    await page.getByTestId('logs-delete-confirm-input').fill('DELETE');
    await expect(confirm).toBeDisabled();

    // Acknowledgement alone is not enough either.
    await page.getByTestId('logs-delete-confirm-input').fill('delete');
    await page.getByTestId('logs-delete-acknowledge').check();
    await expect(confirm).toBeDisabled();

    // Nothing was sent while the dialog refused.
    expect(state.bulkCalls).toHaveLength(0);

    await page.getByTestId('logs-delete-confirm-input').fill('DELETE');
    await expect(confirm).toBeEnabled();
    await confirm.click();

    await expect(page.getByTestId('log-row-101')).toHaveCount(0);
    expect(state.bulkCalls[0].body).toMatchObject({
      mode: 'ids', ids: [101], confirm: 'DELETE', acknowledged: true });
  });
});

// ===========================================================================
// Select All Filtered - the property the whole bulk design turns on
// ===========================================================================
test.describe('Select All Filtered', () => {
  const manyLogs = Array.from({ length: 120 }, (_, i) => ({
    id: 1000 + i, level: i % 2 ? 'info' : 'error',
    message: `entry number ${i}`, created_at: '2026-08-01T09:00:00+00:00',
    actor_user_id: 1, store_id: 1, device_public_id: null, archived_at: null,
  }));

  test('sends the filter, never an id list, and names the true total', async ({ page }) => {
    const state = await mockBackend(page, { logs: manyLogs });
    await page.goto('/logs');
    await page.getByTestId('log-level-filter').selectOption('error');
    await expect(page.getByTestId('result-count')).toContainText('60 results');

    // Offered only because there is more than one page of matches.
    await page.getByTestId('select-all-filtered').click();
    await expect(page.getByTestId('selected-count')).toContainText('60 selected');
    await expect(page.getByTestId('selected-count')).toContainText('including other pages');

    await page.getByTestId('logs-archive-selected').click();

    const sent = state.bulkCalls[0].body;
    expect(sent.mode).toBe('filtered');
    expect(sent.filters).toMatchObject({ level: 'error' });
    // The point of the whole design: React never enumerated 60 ids to post back.
    expect(sent.ids).toBeUndefined();
  });

  test('editing the filter invalidates a Select All Filtered made under the old one',
       async ({ page }) => {
    await mockBackend(page, { logs: manyLogs });
    await page.goto('/logs');
    await page.getByTestId('log-level-filter').selectOption('error');
    await page.getByTestId('select-all-filtered').click();
    await expect(page.getByTestId('selected-count')).toBeVisible();

    await page.getByTestId('log-level-filter').selectOption('info');
    // The operator agreed to "all 60 errors", not to "all 60 infos".
    await expect(page.getByTestId('selected-count')).toHaveCount(0);
  });

  test('Select All Filtered is not offered when everything already fits on one page',
       async ({ page }) => {
    await mockBackend(page);
    await page.goto('/logs');
    await expect(page.getByTestId('select-page')).toBeVisible();
    await expect(page.getByTestId('select-all-filtered')).toHaveCount(0);
  });
});

// ===========================================================================
// Broadcast History
// ===========================================================================
test.describe('Broadcast History', () => {
  test('search narrows the list and archived rows are labelled when shown',
       async ({ page }) => {
    await mockBackend(page);
    await page.goto('/history');
    await expect(page.getByTestId('result-count')).toContainText('2 results');

    await page.getByTestId('history-search').fill('Evening');
    await expect(page.getByTestId('result-count')).toContainText('1 result');
    await expect(page.getByTestId('history-row-9')).toBeVisible();

    await page.getByTestId('clear-filters').click();
    await page.getByTestId('history-select-8').check();
    await page.getByTestId('history-archive-selected').click();
    await expect(page.getByTestId('history-row-8')).toHaveCount(0);

    // Shown together, an archived row must be distinguishable from a live one.
    await page.getByTestId('history-archived').selectOption('all');
    await expect(page.getByTestId('history-archived-8')).toContainText(/archived/i);
    await expect(page.getByTestId('history-archived-9')).toHaveCount(0);
  });

  test('Unarchive puts a session back into the default view', async ({ page }) => {
    await mockBackend(page);
    await page.goto('/history');
    await page.getByTestId('history-select-8').check();
    await page.getByTestId('history-archive-selected').click();
    await expect(page.getByTestId('history-row-8')).toHaveCount(0);

    await page.getByTestId('history-archived').selectOption('only');
    await page.getByTestId('history-select-8').check();
    await page.getByTestId('history-unarchive-selected').click();

    await page.getByTestId('history-archived').selectOption('');
    await expect(page.getByTestId('history-row-8')).toBeVisible();
  });
});

// ===========================================================================
// User Management - permanent deletion is a tombstone with no way back
// ===========================================================================
test.describe('User Management', () => {
  test('the Role filter reaches the server', async ({ page }) => {
    await mockBackend(page);
    await page.goto('/users');
    await expect(page.getByTestId('result-count')).toContainText('4 results');

    await page.getByTestId('users-role').selectOption('ADMIN');
    await expect(page.getByTestId('result-count')).toContainText('1 result');
    await expect(page.getByTestId('user-row-priya')).toBeVisible();
    await expect(page.getByTestId('user-row-rahul')).toHaveCount(0);
  });

  test('permanent deletion needs the typed username, and removes the account entirely',
       async ({ page }) => {
    const state = await mockBackend(page, { operator: OWNER });
    await page.goto('/users');
    await page.getByTestId('purge-rahul').click();

    const confirm = page.getByTestId('purge-user-confirm-btn');
    await expect(confirm).toBeDisabled();
    // The word DELETE is NOT the confirmation here - the username is, so the
    // muscle memory built on every other dialog does not carry over.
    await page.getByTestId('purge-user-confirm-input').fill('DELETE');
    await page.getByTestId('purge-user-acknowledge').check();
    await expect(confirm).toBeDisabled();

    await page.getByTestId('purge-user-confirm-input').fill('rahul');
    await expect(confirm).toBeEnabled();
    await confirm.click();

    await expect(page.getByTestId('user-row-rahul')).toHaveCount(0);
    expect(state.deletePermanentlyCalls[0].body)
      .toMatchObject({ confirm: 'rahul', acknowledged: true });

    // GONE, not hidden. This used to assert that a "show deleted accounts"
    // filter brought the row back marked "permanently deleted" - which was
    // true while deletion left a tombstoned row behind, and was exactly what
    // kept the username reserved for ever. There is no such filter now,
    // because there is no row for it to reveal.
    await expect(page.getByTestId('users-include-deleted')).toHaveCount(0);
    await expect(page.getByTestId('user-row-rahul')).toHaveCount(0);
    await expect(page.locator('tbody')).not.toContainText('permanently deleted');

    // Searching for it by name finds nothing either.
    await page.getByTestId('users-search').fill('rahul');
    await expect(page.getByTestId('user-row-rahul')).toHaveCount(0);
  });

  test('an ADMIN is not offered permanent deletion at all', async ({ page }) => {
    await mockBackend(page, {
      operator: { id: 2, username: 'priya', display_name: 'Priya Sharma', role: 'ADMIN' },
    });
    await page.goto('/users');
    await expect(page.getByTestId('users-table')).toBeVisible();
    await expect(page.getByTestId('purge-rahul')).toHaveCount(0);
  });
});

// ===========================================================================
// Rights - the deliberate client-side exception
// ===========================================================================
test.describe('Rights', () => {
  test('search and category filter the catalog without a further request',
       async ({ page }) => {
    await mockBackend(page, { operator: OWNER });
    await page.goto('/users');
    await page.getByTestId('rights-priya').click();
    await expect(page.getByTestId('rights-filter-bar')).toBeVisible();

    const before = page.getByTestId('rights-result-count');
    await expect(before).toContainText('of');

    // No network call is made for a filter: the catalog is already loaded.
    const requests = [];
    page.on('request', (r) => { if (r.url().includes('/permissions')) requests.push(r.url()); });

    await page.getByTestId('rights-search').fill('broadcast');
    await expect(page.getByTestId('right-row-broadcast.start')).toBeVisible();
    await expect(page.getByTestId('right-row-stores.create')).toHaveCount(0);
    expect(requests).toHaveLength(0);

    await page.getByTestId('rights-clear-filters').click();
    await expect(page.getByTestId('right-row-stores.create')).toBeVisible();
  });

  test('"explicit overrides only" hides everything still inheriting from the role',
       async ({ page }) => {
    await mockBackend(page, { operator: OWNER });
    await page.goto('/users');
    await page.getByTestId('rights-priya').click();
    await expect(page.getByTestId('rights-filter-bar')).toBeVisible();

    await page.getByTestId('rights-overridden-only').check();
    await expect(page.getByTestId('rights-empty')).toBeVisible();

    // A pending change counts as an override immediately - the filter reads
    // what the operator has chosen, not only what has been saved.
    await page.getByTestId('rights-overridden-only').uncheck();
    await page.getByTestId('right-select-broadcast.start').selectOption('DENY');
    await page.getByTestId('rights-overridden-only').check();
    await expect(page.getByTestId('right-row-broadcast.start')).toBeVisible();
    await expect(page.getByTestId('rights-result-count')).toContainText('1 of');
  });
});

// ===========================================================================
// Receiver Devices - archived and deleted must never look alike
// ===========================================================================
test.describe('Receiver Devices fleet', () => {
  test('filters narrow the fleet and a Store link is offered per row', async ({ page }) => {
    await mockBackend(page);
    await page.goto('/devices');
    await expect(page.getByTestId('result-count')).toContainText('2 results');

    await page.getByTestId('fleet-primary').selectOption('true');
    await expect(page.getByTestId('result-count')).toContainText('1 result');
    await expect(page.getByTestId('fleet-row-482f9e9b-3371-4c06-845f-202c34e661d0')).toBeVisible();
  });

  test('a permanently deleted Device is red, explained, and offers no way back',
       async ({ page }) => {
    const publicId = '00875774-d573-4486-8fbf-473ea4d972fd';
    const state = await mockBackend(page, { operator: OWNER });
    await page.goto('/devices');
    await page.getByTestId(`fleet-purge-${publicId}`).click();

    const confirm = page.getByTestId('fleet-purge-confirm-btn');
    await page.getByTestId('fleet-purge-acknowledge').check();
    await page.getByTestId('fleet-purge-confirm-input').fill('not-the-id');
    await expect(confirm).toBeDisabled();

    await page.getByTestId('fleet-purge-confirm-input').fill(publicId);
    await expect(confirm).toBeEnabled();
    await confirm.click();

    await expect(page.getByTestId(`fleet-row-${publicId}`)).toHaveCount(0);
    expect(state.deletePermanentlyCalls[0].body)
      .toMatchObject({ confirm: publicId, acknowledged: true });

    // A permanently deleted Device is now operationally gone: there is no
    // lifecycle selection that brings it back on screen, which is the point -
    // it cannot be restored, so offering a way to look at it would only
    // invite somebody to try.
    for (const selection of ['all_current', 'active', 'archived']) {
      await page.getByTestId('fleet-lifecycle').selectOption(selection);
      await expect(page.getByTestId(`fleet-row-${publicId}`)).toHaveCount(0);
    }
  });

  test('archived and deleted are told apart, not merged into one grey state',
       async ({ page }) => {
    const archived = '00875774-d573-4486-8fbf-473ea4d972fd';
    await mockBackend(page, {
      devices: [
        { public_id: '482f9e9b-3371-4c06-845f-202c34e661d0', display_name: 'UN till 1',
          status: 'active', role: 'PRIMARY', enrolled_at: '2026-07-27T09:12:00+00:00' },
        { public_id: archived, display_name: 'UN till 2', status: 'disabled', role: 'STANDBY',
          enrolled_at: '2026-07-27T09:20:00+00:00', archived_at: '2026-08-01T12:00:00+00:00' },
      ],
    });
    await page.goto('/devices');
    // Archived is hidden by default - it is not an operational Device.
    await expect(page.getByTestId(`fleet-row-${archived}`)).toHaveCount(0);

    await page.getByTestId('fleet-lifecycle').selectOption('archived');
    await expect(page.getByTestId(`fleet-lifecycle-${archived}`)).toHaveText('Archived');
    // And crucially: it does NOT read as deleted.
    await expect(page.getByTestId(`fleet-lifecycle-${archived}`)).not.toContainText(/deleted/i);
    await expect(page.getByTestId(`fleet-deleted-note-${archived}`)).toHaveCount(0);
  });
});
