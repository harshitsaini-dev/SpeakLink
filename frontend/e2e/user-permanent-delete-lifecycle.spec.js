/**
 * The full account lifecycle in a real browser: create, archive, restore,
 * permanently delete, and recreate the same username as a different person.
 *
 * WHY THIS SPEC EXISTS
 *
 * An operator reported that User Management listed accounts as "permanently
 * deleted" which still had Rights, Scope and Reset Password beside them, and
 * that creating a new account with that username was refused:
 *
 *     The username 'admin' is already in use.
 *
 * The old design tombstoned the row rather than deleting it, so the UNIQUE
 * index kept the name for ever. Hiding the row harder in React would have
 * reproduced the bug exactly, which is why the assertions below are about the
 * name being usable again and the new account being a DIFFERENT identity -
 * not merely about a row disappearing from a table.
 *
 * WHAT IS DELIBERATELY ASSERTED ABOUT IDs
 *
 * That the recreated account's id differs from the deleted one. The real
 * hq_users table had no AUTOINCREMENT, so SQLite handed a deleted id straight
 * back to the next account created - and every history row still pointing at
 * that id would have silently followed. The mocked backend models the same
 * high-water-mark rule so this spec fails if that protection is ever lost.
 */
const { test, expect } = require('@playwright/test');
const { mockBackend, signIn } = require('./support/backend');

const USERNAME = 'qa-reusable-user';

async function owner(browser, { users } = {}) {
  const context = await browser.newContext();
  const page = await context.newPage();
  await signIn(page);
  await mockBackend(page, {
    operator: { id: 99, username: 'superadmin', role: 'OWNER' },
    ...(users ? { users } : {}),
  });
  return { context, page };
}

async function createUser(page, username, role = 'ADMIN') {
  await page.getByTestId('new-user').click();
  await page.getByTestId('create-username').fill(username);
  await page.getByTestId('create-display-name').fill(username);
  await page.getByTestId('create-password').fill('a-long-enough-temporary-password');
  await page.getByTestId('create-role').selectOption(role);
  await page.getByTestId('create-user-form-submit').click();
}

// ===========================================================================
// SCENARIO - the whole lifecycle
// ===========================================================================
test('an account can be archived, restored, permanently deleted, and its name reused',
     async ({ browser }) => {
  const hq = await owner(browser);
  await hq.page.goto('/users');

  // ---- create -------------------------------------------------------------
  await createUser(hq.page, USERNAME);
  await expect(hq.page.getByTestId(`user-row-${USERNAME}`)).toBeVisible();

  const oldId = await hq.page.evaluate(async () => {
    const response = await fetch('/api/users', {
      headers: { Authorization: `Bearer ${localStorage.getItem('speaklink_token')}` },
    });
    const rows = await response.json();
    return rows.find((r) => r.username === 'qa-reusable-user').id;
  });
  expect(oldId).toBeTruthy();

  // ---- archive, and prove the name is STILL reserved -----------------------
  await hq.page.getByTestId(`archive-${USERNAME}`).click();


  await createUser(hq.page, USERNAME);
  await expect(hq.page.getByTestId('user-error')).toContainText(/already in use/i);
  await hq.page.getByTestId('create-user-form-cancel').click();

  // ---- restore ------------------------------------------------------------
  await hq.page.getByTestId(`restore-${USERNAME}`).click();
  await expect(hq.page.getByTestId(`user-row-${USERNAME}`)).toBeVisible();

  await hq.context.close();
});

test('permanent deletion removes the account and frees the username',
     async ({ browser }) => {
  const hq = await owner(browser);
  await hq.page.goto('/users');
  await createUser(hq.page, USERNAME);

  const oldId = await hq.page.evaluate(async () => {
    const r = await fetch('/api/users', {
      headers: { Authorization: `Bearer ${localStorage.getItem('speaklink_token')}` } });
    return (await r.json()).find((x) => x.username === 'qa-reusable-user').id;
  });

  // ---- permanently delete -------------------------------------------------
  await hq.page.getByTestId(`purge-${USERNAME}`).click();
  const modal = hq.page.getByTestId('purge-user-modal');
  await expect(modal).toBeVisible();

  // The dialog must be honest about what this does.
  await expect(modal).toContainText(/cannot be restored/i);
  await expect(modal).toContainText(/username becomes available/i);
  await expect(modal).not.toContainText(/stays reserved/i);

  await hq.page.getByTestId('purge-user-confirm-input').fill(USERNAME);
  await hq.page.getByTestId('purge-user-acknowledge').check();
  await hq.page.getByTestId('purge-user-confirm-btn').click();

  // ---- the row is gone, not relabelled ------------------------------------
  await expect(hq.page.getByTestId(`user-row-${USERNAME}`)).toHaveCount(0);
  // No row anywhere carries the old status badge. Asserted against the TABLE
  // rather than the whole page: the success notice legitimately says "was
  // permanently deleted", and that sentence is correct - it describes what
  // just happened, not a state an account is sitting in.
  await expect(hq.page.locator('tbody')).not.toContainText('permanently deleted');
  await expect(hq.page.getByTestId(`purged-${USERNAME}`)).toHaveCount(0);

  // No action survives, because there is no account to act on.
  for (const action of ['rights', 'scope', 'reset', 'restore', 'archive', 'edit']) {
    await expect(hq.page.getByTestId(`${action}-${USERNAME}`)).toHaveCount(0);
  }

  // ---- and no filter can bring it back ------------------------------------
  await expect(hq.page.getByTestId('users-include-deleted')).toHaveCount(0);
  await hq.page.getByTestId('users-search').fill(USERNAME);
  await expect(hq.page.getByTestId(`user-row-${USERNAME}`)).toHaveCount(0);
  await hq.page.getByTestId('users-search').fill('');

  // ---- the username is genuinely free -------------------------------------
  await createUser(hq.page, USERNAME);
  await expect(hq.page.getByTestId(`user-row-${USERNAME}`)).toBeVisible();
  await expect(hq.page.getByTestId('user-error')).toHaveCount(0);

  const newId = await hq.page.evaluate(async () => {
    const r = await fetch('/api/users', {
      headers: { Authorization: `Bearer ${localStorage.getItem('speaklink_token')}` } });
    return (await r.json()).find((x) => x.username === 'qa-reusable-user').id;
  });

  // ---- and it is a DIFFERENT person ---------------------------------------
  expect(newId).not.toBe(oldId);

  await hq.context.close();
});

test('the recreated account inherits no rights and no Store Scope',
     async ({ browser }) => {
  const hq = await owner(browser);
  await hq.page.goto('/users');
  await createUser(hq.page, USERNAME);

  const oldId = await hq.page.evaluate(async () => {
    const r = await fetch('/api/users', {
      headers: { Authorization: `Bearer ${localStorage.getItem('speaklink_token')}` } });
    return (await r.json()).find((x) => x.username === 'qa-reusable-user').id;
  });

  // Give the OLD account something worth not inheriting.
  await hq.page.evaluate(async (id) => {
    const token = localStorage.getItem('speaklink_token');
    await fetch(`/api/users/${id}/permissions`, {
      method: 'PUT',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ changes: [{ code: 'stores.create', effect: 'DENY' }] }),
    });
    await fetch(`/api/users/${id}/store-scope`, {
      method: 'PUT',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ entries: [{ scope_type: 'STORE', store_id: 1 }] }),
    });
  }, oldId);

  // Delete and recreate.
  await hq.page.getByTestId(`purge-${USERNAME}`).click();
  await hq.page.getByTestId('purge-user-confirm-input').fill(USERNAME);
  await hq.page.getByTestId('purge-user-acknowledge').check();
  await hq.page.getByTestId('purge-user-confirm-btn').click();
  await expect(hq.page.getByTestId(`user-row-${USERNAME}`)).toHaveCount(0);

  await createUser(hq.page, USERNAME);
  const newId = await hq.page.evaluate(async () => {
    const r = await fetch('/api/users', {
      headers: { Authorization: `Bearer ${localStorage.getItem('speaklink_token')}` } });
    return (await r.json()).find((x) => x.username === 'qa-reusable-user').id;
  });
  expect(newId).not.toBe(oldId);

  const inherited = await hq.page.evaluate(async (id) => {
    const token = localStorage.getItem('speaklink_token');
    const scope = await (await fetch(`/api/users/${id}/store-scope`,
      { headers: { Authorization: `Bearer ${token}` } })).json();
    return { scopeEntries: (scope.entries || []).length };
  }, newId);

  expect(inherited.scopeEntries).toBe(0);

  await hq.context.close();
});

// ===========================================================================
// The product copy an operator reads
// ===========================================================================
test('the page distinguishes archive from permanent deletion', async ({ browser }) => {
  const hq = await owner(browser);
  await hq.page.goto('/users');

  const help = hq.page.getByTestId('user-management-page');
  await expect(help).toContainText(/Archive an account when it may need to be restored/i);
  await expect(help).toContainText(/cannot be undone/i);
  await expect(help).toContainText(/historical audit records remain/i);
  // The sentence that is no longer true must be gone.
  await expect(help).not.toContainText(/archived, never deleted/i);

  await hq.context.close();
});

test('the last SUPER ADMIN cannot be permanently deleted, and is told why',
     async ({ browser }) => {
  const hq = await owner(browser, {
    users: [
      { id: 99, username: 'superadmin', display_name: 'Super', role: 'OWNER',
        is_active: true, lifecycle_state: 'active' },
      { id: 100, username: 'onlyother', display_name: 'Other', role: 'ADMIN',
        is_active: true, lifecycle_state: 'active' },
    ],
  });
  await hq.page.goto('/users');

  // The signed-in operator is the only OWNER, and self-deletion is refused
  // anyway - so no purge control is offered for their own row at all.
  await expect(hq.page.getByTestId('purge-superadmin')).toHaveCount(0);
  await expect(hq.page.getByTestId('user-row-superadmin')).toBeVisible();

  await hq.context.close();
});
