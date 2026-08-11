/**
 * The full Store lifecycle in a real browser: create, archive, restore,
 * permanently delete, and recreate the same Store Code as a different Store.
 *
 * WHY THIS SPEC EXISTS
 *
 * An operator permanently deleted the Store TESTSTORE. It vanished from the Store
 * list, and then adding a Store with code TESTSTORE was refused:
 *
 *     store_code already exists
 *
 * The old design tombstoned the row, so the UNIQUE index kept the code for
 * ever. Hiding the row harder in React would have reproduced that exactly,
 * which is why the assertions below are about the CODE being usable again and
 * the replacement being a different Store - not merely about a row leaving a
 * table.
 *
 * WHAT IS DELIBERATELY ASSERTED ABOUT IDs
 *
 * That the recreated Store's id differs from the deleted one. The real stores
 * table had no AUTOINCREMENT, and the live tombstones were ids 58, 59 and 60
 * with 60 the maximum - so deleting it and adding any Store would have handed
 * the replacement id 60 and every history row pointing there. The mocked
 * backend models the same high-water-mark rule, so this spec fails if that
 * protection is ever lost.
 */
const { test, expect } = require('@playwright/test');
const { mockBackend, signIn } = require('./support/backend');

const CODE = 'QADEL';

async function owner(browser) {
  const context = await browser.newContext();
  const page = await context.newPage();
  await signIn(page);
  await mockBackend(page, {
    operator: { id: 99, username: 'superadmin', role: 'OWNER' },
  });
  return { context, page };
}

async function storeIdFor(page, code) {
  return page.evaluate(async (wanted) => {
    const response = await fetch('/api/stores?include_archived=true', {
      headers: { Authorization: `Bearer ${localStorage.getItem('echocast_token')}` },
    });
    const rows = await response.json();
    const found = rows.find((r) => r.store_code === wanted);
    return found ? found.id : null;
  }, code);
}

async function createStore(page, code, name) {
  return page.evaluate(async ([storeCode, storeName]) => {
    const response = await fetch('/api/stores', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${localStorage.getItem('echocast_token')}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        store_code: storeCode, store_name: storeName,
        city: 'TESTVILLE', region: 'TEST ZONE',
      }),
    });
    return { status: response.status, body: await response.json() };
  }, [code, name]);
}

// ===========================================================================
// Archive reserves the code; permanent deletion frees it
// ===========================================================================
test('an archived Store keeps its Store Code reserved', async ({ browser }) => {
  const hq = await owner(browser);
  await hq.page.goto('/stores');

  const created = await createStore(hq.page, CODE, 'QA Disposable');
  expect(created.status).toBe(201);
  const oldId = created.body.id;

  await hq.page.reload();
  await expect(hq.page.getByTestId(`store-mgmt-row-${CODE}`)).toBeVisible();

  await hq.page.evaluate(async (id) => {
    await fetch(`/api/stores/${id}/archive`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${localStorage.getItem('echocast_token')}` },
    });
  }, oldId);

  // Archived is restorable, so the code must still be taken.
  const clash = await createStore(hq.page, CODE, 'Impostor');
  expect(clash.status).toBe(409);

  await hq.context.close();
});

test('permanent deletion removes the Store and frees its Store Code',
     async ({ browser }) => {
  const hq = await owner(browser);
  await hq.page.goto('/stores');

  const created = await createStore(hq.page, CODE, 'QA Disposable');
  const oldId = created.body.id;
  await hq.page.reload();

  // ---- permanently delete through the UI ---------------------------------
  await hq.page.getByTestId(`tombstone-store-${CODE}`).click();
  const modal = hq.page.getByTestId('tombstone-store-modal');
  await expect(modal).toBeVisible();

  // The dialog must be honest about what this does.
  await expect(hq.page.getByTestId('tombstone-consequences'))
    .toContainText(/Store Code becomes available/i);
  await expect(hq.page.getByTestId('tombstone-consequences'))
    .toContainText(/different Store/i);

  await hq.page.getByTestId('tombstone-confirm-input').fill(CODE);
  await hq.page.getByTestId('tombstone-acknowledge-checkbox').check();
  await hq.page.getByTestId('tombstone-confirm').click();

  // ---- the row is gone, not relabelled ------------------------------------
  await expect(hq.page.getByTestId(`store-mgmt-row-${CODE}`)).toHaveCount(0);
  await expect(hq.page.locator('tbody')).not.toContainText('permanently deleted');
  expect(await storeIdFor(hq.page, CODE)).toBeNull();

  // Searching for it finds nothing.
  await hq.page.getByTestId('stores-search').fill(CODE);
  await expect(hq.page.getByTestId(`store-mgmt-row-${CODE}`)).toHaveCount(0);
  await hq.page.getByTestId('stores-search').fill('');

  // ---- and the code is genuinely free -------------------------------------
  const recreated = await createStore(hq.page, CODE, 'A Completely Different Shop');
  expect(recreated.status).toBe(201);
  const newId = recreated.body.id;

  // ---- and it is a DIFFERENT Store ----------------------------------------
  expect(newId).not.toBe(oldId);

  await hq.page.reload();
  await expect(hq.page.getByTestId(`store-mgmt-row-${CODE}`)).toBeVisible();
  await expect(hq.page.getByTestId(`store-mgmt-row-${CODE}`))
    .toContainText('A Completely Different Shop');

  await hq.context.close();
});

test('no lifecycle selection can bring a permanently deleted Store back',
     async ({ browser }) => {
  const hq = await owner(browser);
  await hq.page.goto('/stores');
  await createStore(hq.page, CODE, 'QA Disposable');
  await hq.page.reload();

  await hq.page.getByTestId(`tombstone-store-${CODE}`).click();
  await hq.page.getByTestId('tombstone-confirm-input').fill(CODE);
  await hq.page.getByTestId('tombstone-acknowledge-checkbox').check();
  await hq.page.getByTestId('tombstone-confirm').click();
  await expect(hq.page.getByTestId(`store-mgmt-row-${CODE}`)).toHaveCount(0);

  // Every lifecycle the control offers, and a hand-crafted deleted value.
  const control = hq.page.getByTestId('stores-lifecycle');
  for (const value of ['all_current', 'active', 'disabled', 'archived']) {
    await control.selectOption(value);
    await expect(hq.page.getByTestId(`store-mgmt-row-${CODE}`)).toHaveCount(0);
  }

  const forced = await hq.page.evaluate(async () => {
    const r = await fetch('/api/stores/search?lifecycle=deleted&page_size=200', {
      headers: { Authorization: `Bearer ${localStorage.getItem('echocast_token')}` },
    });
    return (await r.json()).items.map((s) => s.store_code);
  });
  expect(forced).not.toContain(CODE);

  await hq.context.close();
});

test('the deleted Store offers no Edit, Archive, Restore or Device action',
     async ({ browser }) => {
  const hq = await owner(browser);
  await hq.page.goto('/stores');
  await createStore(hq.page, CODE, 'QA Disposable');
  await hq.page.reload();

  await hq.page.getByTestId(`tombstone-store-${CODE}`).click();
  await hq.page.getByTestId('tombstone-confirm-input').fill(CODE);
  await hq.page.getByTestId('tombstone-acknowledge-checkbox').check();
  await hq.page.getByTestId('tombstone-confirm').click();
  await expect(hq.page.getByTestId(`store-mgmt-row-${CODE}`)).toHaveCount(0);

  for (const action of ['edit-store', 'archive-store', 'restore-store',
                        'disable-store', 'enable-store', 'devices',
                        'tombstone-store', 'delete-store', 'regen-token']) {
    await expect(hq.page.getByTestId(`${action}-${CODE}`)).toHaveCount(0);
  }

  await hq.context.close();
});

test('the confirmation requires the exact Store Code and an acknowledgement',
     async ({ browser }) => {
  const hq = await owner(browser);
  await hq.page.goto('/stores');
  await createStore(hq.page, CODE, 'QA Disposable');
  await hq.page.reload();

  await hq.page.getByTestId(`tombstone-store-${CODE}`).click();
  const confirm = hq.page.getByTestId('tombstone-confirm');
  await expect(confirm).toBeDisabled();

  await hq.page.getByTestId('tombstone-confirm-input').fill('DELETE');
  await hq.page.getByTestId('tombstone-acknowledge-checkbox').check();
  await expect(confirm).toBeDisabled();

  await hq.page.getByTestId('tombstone-confirm-input').fill(CODE);
  await expect(confirm).toBeEnabled();

  // Cancelling changes nothing.
  await hq.page.getByTestId('tombstone-cancel').click();
  await expect(hq.page.getByTestId(`store-mgmt-row-${CODE}`)).toBeVisible();

  await hq.context.close();
});

test('the dialog never shows a Receiver token or credential', async ({ browser }) => {
  const hq = await owner(browser);
  await hq.page.goto('/stores');
  await hq.page.getByTestId('tombstone-store-UN').click();

  const dialog = await hq.page.getByTestId('tombstone-store-modal').innerText();
  for (const leak of ['echocast_rcv_v1', 'receiver_token', 'credential:']) {
    expect(dialog).not.toContain(leak);
  }

  await hq.context.close();
});
