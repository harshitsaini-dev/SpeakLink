/**
 * Two permission boundaries, in a real browser.
 *
 * BUG 1 - a User without "View Store Management" also lost the Store list in
 * Broadcast Console, because the Console read the administrative Store
 * endpoint. Management and targeting are separate capabilities.
 *
 * BUG 2 - an ADMIN granted "Manage User Rights" still could not manage them:
 * the endpoint required OWNER and the React button was keyed to the role name.
 *
 * These specs drive the rendered page rather than the API, because both
 * defects were only visible as "the thing I was given permission for is not
 * there". A test that called the endpoint directly would have missed the
 * frontend half of Bug 2 entirely.
 */
const { test, expect } = require('@playwright/test');
const { mockBackend, signIn } = require('./support/backend');

const BROADCAST_ONLY = [
  'menu.broadcast.view', 'broadcast.start', 'broadcast.stop',
  'menu.history.view',
];

async function open(browser, options) {
  const context = await browser.newContext();
  const page = await context.newPage();
  await signIn(page);
  await mockBackend(page, options);
  return { context, page };
}

// ===========================================================================
// BUG 1 - Broadcast targets survive a Store Management denial
// ===========================================================================
test('a broadcaster denied Store Management still sees Broadcast targets',
     async ({ browser }) => {
  const hq = await open(browser, {
    operator: { id: 42, username: 'caster', role: 'BROADCASTER' },
    // Exactly the operator's account: no menu.stores.view anywhere.
    permissions: BROADCAST_ONLY,
    // And the administrative endpoint really refuses, as the server would.
    storesListStatus: 403,
  });

  await hq.page.goto('/broadcast');
  // The target table is populated despite /stores being denied.
  await expect(hq.page.getByTestId('store-row-UN')).toBeVisible();
  await expect(hq.page.locator('tbody tr').first()).toBeVisible();

  await hq.context.close();
});

test('Store Management stays denied for that same broadcaster',
     async ({ browser }) => {
  const hq = await open(browser, {
    operator: { id: 42, username: 'caster', role: 'BROADCASTER' },
    permissions: BROADCAST_ONLY,
    storesListStatus: 403,
  });

  await hq.page.goto('/stores');
  // Routing sends an account without menu.stores.view away from the page;
  // whichever way it resolves, the management table must not be on screen.
  await expect(hq.page.getByTestId('store-mgmt-row-UN')).toHaveCount(0);

  await hq.context.close();
});

test('an out-of-scope Store is absent from the target response itself',
     async ({ browser }) => {
  const hq = await open(browser, {
    operator: { id: 42, username: 'caster', role: 'BROADCASTER' },
    permissions: BROADCAST_ONLY,
    scopedStoreIds: [],           // scoped to nothing, never widened
  });

  await hq.page.goto('/broadcast');
  // Crafted directly, bypassing the UI entirely: the backend answer is empty,
  // so there is nothing for a client-side filter to have hidden.
  const codes = await hq.page.evaluate(async () => {
    const r = await fetch('/api/broadcast/target-stores', {
      headers: { Authorization: `Bearer ${localStorage.getItem('echocast_token')}` },
    });
    return (await r.json()).stores.map((s) => s.store_code);
  });
  expect(codes).toEqual([]);

  await hq.context.close();
});

test('the target catalog leaks no Receiver or administrative field',
     async ({ browser }) => {
  const hq = await open(browser, {
    operator: { id: 42, username: 'caster', role: 'BROADCASTER' },
    permissions: BROADCAST_ONLY,
  });
  await hq.page.goto('/broadcast');

  const raw = await hq.page.evaluate(async () => {
    const r = await fetch('/api/broadcast/target-stores', {
      headers: { Authorization: `Bearer ${localStorage.getItem('echocast_token')}` },
    });
    return JSON.stringify(await r.json());
  });
  for (const leak of ['receiver_token', 'echocast_rcv_v1', 'credential',
                      'enrollment', 'deleted_at']) {
    expect(raw.toLowerCase()).not.toContain(leak);
  }

  await hq.context.close();
});

test('Store Management alone does not grant Broadcast targeting',
     async ({ browser }) => {
  const hq = await open(browser, {
    operator: { id: 43, username: 'storeadmin', role: 'VIEWER' },
    permissions: ['menu.stores.view', 'menu.users.view'],
  });
  await hq.page.goto('/broadcast');

  const status = await hq.page.evaluate(async () => {
    const r = await fetch('/api/broadcast/target-stores', {
      headers: { Authorization: `Bearer ${localStorage.getItem('echocast_token')}` },
    });
    return r.status;
  });
  expect(status).toBe(403);

  await hq.context.close();
});

// ===========================================================================
// BUG 2 - Manage User Rights reaches the ADMIN it was granted to
// ===========================================================================
test('an ADMIN holding Manage User Rights sees and opens the Rights editor',
     async ({ browser }) => {
  const hq = await open(browser, {
    operator: { id: 50, username: 'boss', role: 'ADMIN' },
    permissions: ['menu.users.view', 'users.create', 'users.update',
                  'users.disable', 'users.permissions.manage'],
  });

  await hq.page.goto('/users');
  const rights = hq.page.getByTestId('rights-rahul');
  await expect(rights).toBeVisible();

  await rights.click();
  await expect(hq.page.getByTestId('rights-editor')).toBeVisible();

  await hq.context.close();
});

test('an ADMIN without the permission gets no Rights control',
     async ({ browser }) => {
  const hq = await open(browser, {
    operator: { id: 50, username: 'boss', role: 'ADMIN' },
    permissions: ['menu.users.view', 'users.create', 'users.update',
                  'users.disable'],
  });

  await hq.page.goto('/users');
  await expect(hq.page.getByTestId('rights-rahul')).toHaveCount(0);

  await hq.context.close();
});

test('a SUPER ADMIN row never offers Rights to an ADMIN', async ({ browser }) => {
  const hq = await open(browser, {
    operator: { id: 50, username: 'boss', role: 'ADMIN' },
    permissions: ['menu.users.view', 'users.permissions.manage'],
  });

  await hq.page.goto('/users');
  // The protected account is never a rights target, however the row renders.
  await expect(hq.page.getByTestId('rights-founder')).toHaveCount(0);

  await hq.context.close();
});
