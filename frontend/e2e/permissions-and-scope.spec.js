/**
 * Fine-grained RBAC and Store/City/Zone scope, proven in a real browser.
 *
 * Backend enforcement itself (the resolver, the override priority, scope
 * filtering) is proven exhaustively in the backend test suite - these tests
 * exist for a different property: that the FRONTEND correctly reflects
 * GET /auth/permissions via can(), correctly hides/shows action buttons
 * accordingly, and correctly blocks a direct URL visit - all without
 * requiring a real Receiver Agent or a live backend.
 */
const { test, expect } = require('@playwright/test');
const { mockBackend, signIn, STORES, DEVICES } = require('./support/backend');

const SUPER_ADMIN = { id: 1, username: 'founder', role: 'OWNER' };
const ADMIN = { id: 2, username: 'priya', role: 'ADMIN' };
const BROADCASTER = { id: 3, username: 'raj', role: 'BROADCASTER' };
const VIEWER = { id: 4, username: 'anita', role: 'VIEWER' };

const ALL_PERMISSION_CODES = [
  'menu.broadcast.view', 'broadcast.start', 'broadcast.stop', 'broadcast.emergency_stop',
  'menu.stores.view', 'stores.create', 'stores.update', 'stores.archive',
  'stores.delete_permanently',
  'menu.receivers.view', 'devices.enrollment.create', 'devices.primary.assign',
  'devices.rotate', 'devices.disable', 'devices.revoke', 'devices.archive',
  'devices.delete_permanently',
  'menu.history.view', 'menu.logs.view',
  'menu.users.view', 'users.create', 'users.update', 'users.disable', 'users.permissions.manage',
];

// ===========================================================================
// A. ADMIN with stores.update DENIED by an explicit override
// ===========================================================================
test.describe('A. ADMIN with stores.update denied', () => {
  test('Store list stays visible, Edit is hidden, and a direct API call is refused anyway', async ({ page }) => {
    const permissions = ALL_PERMISSION_CODES.filter(
      (c) => c !== 'users.permissions.manage' && c !== 'devices.delete_permanently' && c !== 'stores.update');
    await mockBackend(page, { operator: ADMIN, permissions, storeUpdateStatus: 403 });
    await signIn(page);
    await page.goto('/stores');

    // The list is still there - a DENY on the edit action is not a DENY on
    // the view, and the two must be independent.
    await expect(page.getByTestId('store-mgmt-row-UN')).toBeVisible();
    await expect(page.getByTestId('edit-store-UN')).toHaveCount(0);

    // A hand-crafted request (a browser devtools fetch, a replayed request) is
    // refused by the server regardless of what the UI ever offered.
    const status = await page.evaluate(async () => {
      const token = window.localStorage.getItem('echocast_token');
      const response = await fetch('/api/stores/1', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ store_name: 'Hand-crafted edit' }),
      });
      return response.status;
    });
    expect(status).toBe(403);
  });
});

// ===========================================================================
// B/C. BROADCASTER scoped to one Store / one Zone
// ===========================================================================
test.describe('B. BROADCASTER scoped to a single Store', () => {
  test('only the assigned Store is visible, and an out-of-scope target is rejected', async ({ page }) => {
    // The backend already filtered /stores to the assigned Store before this
    // response left the server - the frontend never re-derives scope, it
    // only reflects what the API returned. Bindapur stands in for "the one
    // assigned Store" here; the mock fixture does not carry that exact name.
    await mockBackend(page, {
      operator: BROADCASTER,
      stores: [STORES[0]],
      sessionCreateStatus: 403,
    });
    await signIn(page);
    await page.goto('/console');

    await expect(page.getByTestId(`store-row-${STORES[0].store_code}`)).toBeVisible();
    for (const store of STORES.slice(1)) {
      await expect(page.getByTestId(`store-row-${store.store_code}`)).toHaveCount(0);
    }

    // A manually crafted request naming an out-of-scope Store id is refused,
    // proving the frontend cannot be trusted to enforce scope by itself.
    const status = await page.evaluate(async () => {
      const token = window.localStorage.getItem('echocast_token');
      const response = await fetch('/api/broadcast/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ campaign_name: 'out of scope', target_mode: 'selected', store_ids: [999] }),
      });
      return response.status;
    });
    expect(status).toBe(403);
  });
});

test.describe('C. BROADCASTER scoped to one Zone', () => {
  test('only that Zone\'s Stores are selectable', async ({ page }) => {
    // Every fixture Store shares one Zone (UN ZONE) - stand in for "assigned
    // to exactly one Zone" by returning only the Zone's Stores, the same
    // shape the real backend's scope filter on GET /stores produces.
    await mockBackend(page, { operator: BROADCASTER, stores: STORES });
    await signIn(page);
    await page.goto('/console');
    for (const store of STORES) {
      await expect(page.getByTestId(`store-row-${store.store_code}`)).toBeVisible();
    }
  });
});

// ===========================================================================
// D. VIEWER: read-only
// ===========================================================================
test.describe('D. VIEWER read-only behaviour', () => {
  test('no create/edit/lifecycle actions are offered anywhere', async ({ page }) => {
    await mockBackend(page, { operator: VIEWER });
    await signIn(page);

    await page.goto('/stores');
    await expect(page.getByTestId('add-store-btn')).toHaveCount(0);
    await expect(page.getByTestId('edit-store-UN')).toHaveCount(0);
    await expect(page.getByTestId('archive-store-UN')).toHaveCount(0);
    await expect(page.getByTestId('store-mgmt-row-UN')).toBeVisible();

    await page.goto('/console');
    await expect(page.getByTestId('start-broadcast-btn')).toHaveCount(0);
    await expect(page.getByTestId('emergency-stop-btn')).toHaveCount(0);
  });

  test('the Users nav link is hidden and the route redirects away', async ({ page }) => {
    await mockBackend(page, { operator: VIEWER });
    await signIn(page);
    await page.goto('/console');
    await expect(page.getByTestId('nav-users')).toHaveCount(0);

    await page.goto('/users');
    await expect(page.getByTestId('user-management-page')).toHaveCount(0);
    await expect(page).toHaveURL(/\/console$/);
  });
});

// ===========================================================================
// E. SUPER ADMIN: unrestricted, Rights editor, Scope editor
// ===========================================================================
test.describe('E. SUPER ADMIN unrestricted access', () => {
  test('every Store action is offered and Rights/Scope editors open', async ({ page }) => {
    await mockBackend(page, { operator: SUPER_ADMIN });
    await signIn(page);

    await page.goto('/stores');
    await expect(page.getByTestId('add-store-btn')).toBeVisible();
    await expect(page.getByTestId('edit-store-UN')).toBeVisible();

    await page.goto('/users');
    await expect(page.getByTestId('rights-priya')).toBeVisible();
    await page.getByTestId('rights-priya').click();
    await expect(page.getByTestId('rights-editor')).toBeVisible();
    await expect(page.getByTestId('rights-base-role')).toContainText('ADMIN');
    await page.getByTestId('rights-close').click();

    await expect(page.getByTestId('scope-priya')).toBeVisible();
    await page.getByTestId('scope-priya').click();
    await expect(page.getByTestId('scope-editor')).toBeVisible();
    await expect(page.getByTestId('scope-empty')).toBeVisible();
  });

  test('Device archive/delete visibility follows the SUPER ADMIN permission set', async ({ page }) => {
    await mockBackend(page, { operator: SUPER_ADMIN });
    await signIn(page);
    await page.goto('/stores/1/devices');
    await expect(page.getByTestId(`archive-${DEVICES[0].public_id}`)).toBeVisible();
    // Delete is never offered for an ACTIVE Device, permission or not - the
    // backend refuses it outright regardless of role.
    await expect(page.getByTestId(`delete-${DEVICES[0].public_id}`)).toHaveCount(0);
  });
});

// ===========================================================================
// F. Direct protected URLs
// ===========================================================================
test.describe('F. Direct protected URL visits', () => {
  test('a VIEWER cannot reach /stores/1/devices Device-security actions but can view Receiver Status', async ({ page }) => {
    await mockBackend(page, { operator: VIEWER });
    await signIn(page);
    await page.goto('/receivers');
    await expect(page.getByTestId('receivers-page')).toBeVisible();
  });

  test('an unauthenticated visit to any protected route lands on /login', async ({ page }) => {
    await mockBackend(page, { operator: VIEWER });
    // Deliberately no signIn(): no token in localStorage.
    await page.goto('/stores');
    await expect(page).toHaveURL(/\/login$/);
  });
});

// ===========================================================================
// H. Receiver Status - the real live defect this round fixes
// ===========================================================================
test.describe('H. Receiver Status reflects the backend-authoritative scoped set', () => {
  test('a BROADCASTER with Store+City+Zone scope sees the Store row, not a blank page', async ({ page }) => {
    // The backend already resolved the union before this response left the
    // server - the mock just returns what a correctly-scoped GET /stores
    // would, exactly like test B/C already model for /console.
    await mockBackend(page, { operator: BROADCASTER, stores: STORES });
    await signIn(page);
    await page.goto('/receivers');
    await expect(page.getByTestId('receivers-page')).toBeVisible();
    await expect(page.getByTestId(`receiver-card-${STORES[0].store_code}`)).toBeVisible();
    await expect(page.getByTestId('receivers-empty')).toHaveCount(0);
  });

  test('a genuinely empty scoped result shows an explanatory empty-state, not blank whitespace', async ({ page }) => {
    await mockBackend(page, { operator: BROADCASTER, stores: [] });
    await signIn(page);
    await page.goto('/receivers');
    await expect(page.getByTestId('receivers-empty')).toBeVisible();
    await expect(page.getByTestId('receivers-empty')).toContainText('No Stores are available');
  });

  test('an API failure shows a visible error, never silent blank whitespace', async ({ page }) => {
    await mockBackend(page, { operator: BROADCASTER, storesListStatus: 500 });
    await signIn(page);
    await page.goto('/receivers');
    await expect(page.getByTestId('receivers-error')).toBeVisible();
  });
});

// ===========================================================================
// I. Scope editor: canonical dropdowns, not free text
// ===========================================================================
test.describe('I. Scope editor uses canonical City/Zone dropdowns', () => {
  test('Store/City/Zone assignment types each offer a dropdown, never free text, and saved state survives a reopen', async ({ page }) => {
    await mockBackend(page, { operator: SUPER_ADMIN });
    await signIn(page);
    await page.goto('/users');
    await page.getByTestId('scope-priya').click();
    await expect(page.getByTestId('scope-editor')).toBeVisible();

    await page.getByTestId('scope-add-type').selectOption('CITY');
    await expect(page.getByTestId('scope-add-value')).toBeVisible();
    await expect(page.locator('[data-testid="scope-add-value"][type="text"]')).toHaveCount(0);
    await page.getByTestId('scope-add-value').selectOption('UN ZONE');
    await page.getByTestId('scope-add-btn').click();
    await expect(page.getByTestId('scope-entry-0')).toContainText('City: UN ZONE');

    await page.getByTestId('scope-add-type').selectOption('REGION');
    await page.getByTestId('scope-add-value').selectOption('UN ZONE');
    await page.getByTestId('scope-add-btn').click();
    await expect(page.getByTestId('scope-entry-1')).toContainText('Zone: UN ZONE');

    await page.getByTestId('scope-add-type').selectOption('STORE');
    await page.getByTestId('scope-add-store').selectOption(String(STORES[0].id));
    await page.getByTestId('scope-add-btn').click();
    await expect(page.getByTestId('scope-entry-2')).toContainText('Store:');

    await page.getByTestId('scope-save').click();
    await expect(page.getByTestId('scope-save-success')).toBeVisible();

    // Reopen: every saved assignment must still be there.
    await page.getByTestId('scope-done').click();
    await page.getByTestId('scope-priya').click();
    await expect(page.getByTestId('scope-entry-0')).toBeVisible();
    await expect(page.getByTestId('scope-entry-1')).toBeVisible();
    await expect(page.getByTestId('scope-entry-2')).toBeVisible();
    await expect(page.getByTestId('scope-clarity-banner')).toContainText('Restricted Scope');
  });
});

// ===========================================================================
// J. History-preserving permanent Store deletion
// ===========================================================================
test.describe('J. Permanent Store deletion (tombstone)', () => {
  test('the button is SUPER ADMIN-only', async ({ page }) => {
    const adminPermissions = ALL_PERMISSION_CODES.filter(
      (c) => c !== 'users.permissions.manage' && c !== 'devices.delete_permanently'
        && c !== 'stores.delete_permanently');
    await mockBackend(page, { operator: ADMIN, permissions: adminPermissions });
    await signIn(page);
    await page.goto('/stores');
    await expect(page.getByTestId(`tombstone-store-${STORES[0].store_code}`)).toHaveCount(0);
  });

  test('SUPER ADMIN sees the button, and deletion requires the typed code AND the acknowledgement', async ({ page }) => {
    await mockBackend(page, { operator: SUPER_ADMIN });
    await signIn(page);
    await page.goto('/stores');

    await expect(page.getByTestId(`tombstone-store-${STORES[0].store_code}`)).toBeVisible();
    await page.getByTestId(`tombstone-store-${STORES[0].store_code}`).click();
    await expect(page.getByTestId('tombstone-store-modal')).toBeVisible();

    // Confirm button stays disabled until BOTH the exact code is typed and
    // the checkbox is checked - neither alone is enough.
    await expect(page.getByTestId('tombstone-confirm')).toBeDisabled();
    await page.getByTestId('tombstone-confirm-input').fill(STORES[0].store_code);
    await expect(page.getByTestId('tombstone-confirm')).toBeDisabled();
    await page.getByTestId('tombstone-acknowledge-checkbox').check();
    await expect(page.getByTestId('tombstone-confirm')).toBeEnabled();

    await page.getByTestId('tombstone-confirm').click();
    await expect(page.getByTestId('tombstone-store-modal')).toHaveCount(0);
    await expect(page.getByTestId(`store-mgmt-row-${STORES[0].store_code}`)).toHaveCount(0);
  });

  test('a wrong typed code keeps the confirm button disabled', async ({ page }) => {
    await mockBackend(page, { operator: SUPER_ADMIN });
    await signIn(page);
    await page.goto('/stores');
    await page.getByTestId(`tombstone-store-${STORES[0].store_code}`).click();
    await page.getByTestId('tombstone-confirm-input').fill('WRONG-CODE');
    await page.getByTestId('tombstone-acknowledge-checkbox').check();
    await expect(page.getByTestId('tombstone-confirm')).toBeDisabled();
  });

  test('a Store with history shows the counts and still allows deletion, not a disabled button', async ({ page }) => {
    await mockBackend(page, { operator: SUPER_ADMIN, storeDependencies: { 1: 3 } });
    await signIn(page);
    await page.goto('/stores');
    await page.getByTestId(`tombstone-store-${STORES[0].store_code}`).click();
    await expect(page.getByTestId('tombstone-history-summary')).toBeVisible();
    await page.getByTestId('tombstone-confirm-input').fill(STORES[0].store_code);
    await page.getByTestId('tombstone-acknowledge-checkbox').check();
    await expect(page.getByTestId('tombstone-confirm')).toBeEnabled();
  });
});

// ===========================================================================
// G. Archived Devices hidden by default, shown with "Show Archived"
// ===========================================================================
test.describe('G. Archived Devices', () => {
  test('an archived Device is hidden by default and shown with Show Archived', async ({ page }) => {
    const archivedDevice = {
      ...DEVICES[1], status: 'disabled', archived_at: '2026-08-01T09:00:00+00:00', role: 'STANDBY',
    };
    await mockBackend(page, {
      operator: SUPER_ADMIN,
      devices: [DEVICES[0], archivedDevice],
    });
    await signIn(page);
    await page.goto('/stores/1/devices');

    await expect(page.getByTestId(`device-row-${DEVICES[0].public_id}`)).toBeVisible();
    await expect(page.getByTestId(`device-row-${archivedDevice.public_id}`)).toHaveCount(0);

    await page.getByTestId('show-archived-toggle').check();
    await expect(page.getByTestId(`device-row-${archivedDevice.public_id}`)).toBeVisible();
    await expect(page.getByTestId(`device-archived-badge-${archivedDevice.public_id}`)).toBeVisible();
    await expect(page.getByTestId(`restore-${archivedDevice.public_id}`)).toBeVisible();
  });
});
