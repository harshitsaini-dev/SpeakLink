/**
 * The Active Broadcasts supervision page, in a real browser.
 *
 * WHAT THIS PROVES THAT UNIT TESTS CANNOT
 *
 * Four operators with four different permission sets, each looking at the
 * same live estate and each seeing a different, correct amount of it. The
 * unit tests prove the components behave; this proves the whole stack agrees
 * - navigation, route guard, list, drawer and confirmation - for a person
 * actually clicking.
 *
 * It also proves the scale property the operator asked for: with fifty
 * concurrent broadcasts the Console must not grow, and this page must page
 * rather than render fifty rows.
 *
 * SEPARATE BROWSER CONTEXTS
 *
 * One per operator, so each has its own storage and its own signed-in
 * identity. Two pages in one context would share a token and would not be two
 * operators at all.
 *
 * WHAT IS DELIBERATELY NOT ASSERTED
 *
 * That any speaker fell silent. These drive commands and read screens.
 */
const { test, expect } = require('@playwright/test');
const { mockBackend, signIn } = require('./support/backend');

const BASE = ['menu.broadcast.view', 'broadcast.start', 'broadcast.stop'];

const ALICE = {
  session_id: 31, campaign_name: 'Alice Diwali Offers',
  owner_user_id: 11, owner_username: 'alice', owner_display_name: 'Alice Kumar',
  started_at: '2026-08-03T10:00:00+00:00',
  target_store_ids: [1], target_store_names: ['Uttam Nagar Old', 'UN'],
};
const BOB = {
  session_id: 32, campaign_name: 'Bob Evening Reminder',
  owner_user_id: 22, owner_username: 'bob', owner_display_name: 'Bob Singh',
  started_at: '2026-08-03T10:05:00+00:00',
  target_store_ids: [2, 5], target_store_names: ['Uttam Nagar ASR', 'Dwarka Mor'],
};
const CAROL = {
  session_id: 33, campaign_name: 'Carol Closing Time',
  owner_user_id: 33, owner_username: 'carol', owner_display_name: 'Carol Rao',
  started_at: '2026-08-03T10:09:00+00:00',
  target_store_ids: [1], target_store_names: ['Uttam Nagar Old'],
};

const THREE = [ALICE, BOB, CAROL];

async function operator(browser, { username = 'supervisor', permissions, activeSessions }) {
  const context = await browser.newContext();
  const page = await context.newPage();
  await signIn(page);
  await mockBackend(page, {
    operator: { id: 99, username, role: 'ADMIN' },
    permissions,
    activeSessions: activeSessions || [],
  });
  return { context, page };
}

// ===========================================================================
// SCENARIO 1 - the four operators of the brief
// ===========================================================================
test('Operator A (active_view only) sees the list and nothing privileged',
     async ({ browser }) => {
  const a = await operator(browser, {
    permissions: [...BASE, 'broadcast.active_view'], activeSessions: THREE,
  });
  await a.page.goto('/active-broadcasts');

  // The page opens and lists every live broadcast.
  await expect(a.page.getByTestId('active-broadcasts-page')).toBeVisible();
  await expect(a.page.getByTestId('active-row-31')).toBeVisible();
  await expect(a.page.getByTestId('active-total')).toHaveText('3');

  // But no broadcaster, no Stores, no Stop.
  await expect(a.page.getByTestId('col-broadcaster')).toHaveCount(0);
  await expect(a.page.getByTestId('active-view-stores-31')).toHaveCount(0);
  await expect(a.page.getByTestId('active-stop-31')).toHaveCount(0);

  const body = (await a.page.textContent('body')).toLowerCase();
  for (const leak of ['alice kumar', 'bob singh', 'carol rao',
                      'uttam nagar', 'dwarka mor']) {
    expect(body).not.toContain(leak);
  }

  await a.context.close();
});

test('Operator B (+ view_ownership) sees the broadcaster but not the Stores',
     async ({ browser }) => {
  const b = await operator(browser, {
    permissions: [...BASE, 'broadcast.active_view', 'broadcast.view_ownership'],
    activeSessions: THREE,
  });
  await b.page.goto('/active-broadcasts');

  await expect(b.page.getByTestId('col-broadcaster')).toBeVisible();
  await expect(b.page.getByTestId('active-owner-cell-31')).toContainText('Alice Kumar');

  // Still no Store identity and no Stop.
  await expect(b.page.getByTestId('active-view-stores-31')).toHaveCount(0);
  await expect(b.page.getByTestId('active-stop-31')).toHaveCount(0);
  expect((await b.page.textContent('body')).toLowerCase()).not.toContain('dwarka mor');

  await b.context.close();
});

test('Operator C (+ view_targets) sees the Stores but not the broadcaster',
     async ({ browser }) => {
  const c = await operator(browser, {
    permissions: [...BASE, 'broadcast.active_view', 'broadcast.view_targets'],
    activeSessions: THREE,
  });
  await c.page.goto('/active-broadcasts');

  await expect(c.page.getByTestId('col-broadcaster')).toHaveCount(0);
  await c.page.getByTestId('active-view-stores-32').click();

  const modal = c.page.getByTestId('active-stores-modal');
  await expect(modal).toBeVisible();
  await expect(modal).toContainText('ASR');
  await expect(modal).toContainText('Uttam Nagar ASR');
  await expect(modal).toContainText('Dwarka Mor');
  // The Stores are visible; whose broadcast it is remains hidden.
  await expect(modal).not.toContainText('Bob Singh');

  await c.context.close();
});

test('Operator D (full supervision) sees everything and may Stop',
     async ({ browser }) => {
  const d = await operator(browser, {
    permissions: [...BASE, 'broadcast.active_view', 'broadcast.view_ownership',
                  'broadcast.view_targets', 'broadcast.stop_any'],
    activeSessions: THREE,
  });
  await d.page.goto('/active-broadcasts');

  await expect(d.page.getByTestId('active-owner-cell-32')).toContainText('Bob Singh');
  await expect(d.page.getByTestId('active-view-stores-32')).toBeVisible();
  await expect(d.page.getByTestId('active-stop-32')).toBeVisible();

  await d.context.close();
});

// ===========================================================================
// SCENARIO 2 - stop Bob, and only Bob
// ===========================================================================
test('a selected Stop ends exactly one broadcast and leaves the others live',
     async ({ browser }) => {
  const d = await operator(browser, {
    permissions: [...BASE, 'broadcast.active_view', 'broadcast.view_ownership',
                  'broadcast.view_targets', 'broadcast.stop_any'],
    activeSessions: THREE,
  });
  await d.page.goto('/active-broadcasts');
  await expect(d.page.getByTestId('active-total')).toHaveText('3');

  await d.page.getByTestId('active-stop-32').click();

  // The confirmation says STOP THIS BROADCAST and never Emergency.
  const modal = d.page.getByTestId('active-stop-modal');
  await expect(modal).toContainText('Stop this broadcast?');
  await expect(modal).not.toContainText(/emergency/i);
  await expect(d.page.getByTestId('stop-modal-owner')).toContainText('Bob Singh');

  await d.page.getByTestId('active-stop-confirm').click();

  // Bob is gone; Alice and Carol are untouched.
  await expect(d.page.getByTestId('active-row-32')).toHaveCount(0);
  await expect(d.page.getByTestId('active-row-31')).toBeVisible();
  await expect(d.page.getByTestId('active-row-33')).toBeVisible();
  await expect(d.page.getByTestId('active-total')).toHaveText('2');

  // And Bob's Stores are released - they are no longer busy on the Console.
  await d.page.goto('/console');
  await expect(d.page.getByTestId('store-busy-ASR')).toHaveCount(0);
  await expect(d.page.getByTestId('store-checkbox-ASR')).toBeEnabled();
  // Alice still holds hers.
  await expect(d.page.getByTestId('store-busy-UN')).toBeVisible();

  await d.context.close();
});

test('Stop is offered without view_targets, and names no Store', async ({ browser }) => {
  const d = await operator(browser, {
    permissions: [...BASE, 'broadcast.active_view', 'broadcast.stop_any'],
    activeSessions: THREE,
  });
  await d.page.goto('/active-broadcasts');
  await d.page.getByTestId('active-stop-32').click();

  const modal = d.page.getByTestId('active-stop-modal');
  await expect(d.page.getByTestId('stop-modal-store-count')).toContainText('2');
  await expect(modal).not.toContainText('Dwarka Mor');
  await expect(modal).not.toContainText('ASR');
  // No owner either - stop_any confers neither disclosure.
  await expect(d.page.getByTestId('stop-modal-owner')).toHaveCount(0);
  await expect(modal).not.toContainText('Bob Singh');

  await d.context.close();
});

// ===========================================================================
// SCENARIO 3 - navigation and the direct-URL boundary
// ===========================================================================
test('the nav item is hidden without broadcast.active_view', async ({ browser }) => {
  const plain = await operator(browser, { permissions: BASE, activeSessions: THREE });
  await plain.page.goto('/console');

  await expect(plain.page.getByTestId('nav-active-broadcasts')).toHaveCount(0);
  await plain.context.close();
});

test('a direct URL visit without the permission is refused', async ({ browser }) => {
  const plain = await operator(browser, { permissions: BASE, activeSessions: THREE });
  await plain.page.goto('/active-broadcasts');

  // Redirected away, and the supervision page never renders.
  await expect(plain.page.getByTestId('active-broadcasts-page')).toHaveCount(0);
  await expect(plain.page).not.toHaveURL(/active-broadcasts/);
  await plain.context.close();
});

test('the nav item appears with the permission and reaches the page',
     async ({ browser }) => {
  const supervisor = await operator(browser, {
    permissions: [...BASE, 'broadcast.active_view'], activeSessions: THREE,
  });
  await supervisor.page.goto('/console');

  await supervisor.page.getByTestId('nav-active-broadcasts').click();
  await expect(supervisor.page.getByTestId('active-broadcasts-page')).toBeVisible();
  await supervisor.context.close();
});

// ===========================================================================
// SCENARIO 4 - fifty concurrent broadcasts
// ===========================================================================
function fifty() {
  return Array.from({ length: 50 }, (_, index) => ({
    session_id: 100 + index,
    campaign_name: `Campaign ${index + 1}`,
    owner_user_id: 200 + index,
    owner_username: `op${index}`,
    owner_display_name: `Operator ${index}`,
    started_at: `2026-08-03T${String(9 + Math.floor(index / 60)).padStart(2, '0')}:${String(index % 60).padStart(2, '0')}:00+00:00`,
    target_store_ids: [1],
    target_store_names: ['Uttam Nagar Old'],
  }));
}

test('Broadcast Console stays compact with fifty live broadcasts',
     async ({ browser }) => {
  const supervisor = await operator(browser, {
    permissions: [...BASE, 'broadcast.active_view', 'broadcast.view_ownership'],
    activeSessions: fifty(),
  });
  await supervisor.page.goto('/console');

  // One badge, one number, one link - and NOT fifty rows.
  await expect(supervisor.page.getByTestId('active-broadcasts-badge')).toBeVisible();
  await expect(supervisor.page.getByTestId('active-broadcasts-count')).toHaveText('50');
  await expect(supervisor.page.getByTestId('active-broadcasts-panel')).toHaveCount(0);

  // The measurable version of "must not grow": the badge occupies a small,
  // bounded height whatever the count says.
  const box = await supervisor.page.getByTestId('active-broadcasts-badge').boundingBox();
  expect(box.height).toBeLessThan(120);

  // And no operator name has leaked onto the Console.
  const body = await supervisor.page.textContent('body');
  expect(body).not.toContain('Operator 7');

  await supervisor.context.close();
});

test('the Active Broadcasts page pages fifty broadcasts rather than rendering them',
     async ({ browser }) => {
  const supervisor = await operator(browser, {
    permissions: [...BASE, 'broadcast.active_view', 'broadcast.view_ownership'],
    activeSessions: fifty(),
  });
  await supervisor.page.goto('/active-broadcasts');

  await expect(supervisor.page.getByTestId('active-total')).toHaveText('50');
  // 20 per page by default, so 20 rows on screen - not 50.
  await expect(supervisor.page.locator('tbody tr')).toHaveCount(20);
  await expect(supervisor.page.getByTestId('active-page-info')).toContainText('Page 1 of 3');

  await supervisor.page.getByTestId('active-next').click();
  await expect(supervisor.page.getByTestId('active-page-info')).toContainText('Page 2 of 3');
  await expect(supervisor.page.locator('tbody tr')).toHaveCount(20);

  // 50 per page is offered and is applied by the SERVER.
  await supervisor.page.getByTestId('active-page-size').selectOption('50');
  await expect(supervisor.page.locator('tbody tr')).toHaveCount(50);

  await supervisor.context.close();
});

// ===========================================================================
// SCENARIO 5 - search and filters follow the permissions
// ===========================================================================
test('search narrows the list on the server', async ({ browser }) => {
  const supervisor = await operator(browser, {
    permissions: [...BASE, 'broadcast.active_view', 'broadcast.view_ownership'],
    activeSessions: THREE,
  });
  await supervisor.page.goto('/active-broadcasts');

  await supervisor.page.getByTestId('active-search').fill('Carol');
  await expect(supervisor.page.getByTestId('active-row-33')).toBeVisible();
  await expect(supervisor.page.getByTestId('active-row-31')).toHaveCount(0);

  await supervisor.context.close();
});

test('a Store search finds nothing for an operator who may not see Stores',
     async ({ browser }) => {
  const blind = await operator(browser, {
    permissions: [...BASE, 'broadcast.active_view'], activeSessions: THREE,
  });
  await blind.page.goto('/active-broadcasts');

  // The name of a Store Bob is broadcasting to. Without view_targets this
  // must match NOTHING - a single result would answer the question the
  // permission refused.
  await blind.page.getByTestId('active-search').fill('Dwarka Mor');
  await expect(blind.page.getByTestId('active-empty')).toBeVisible();

  await blind.context.close();
});

test('the Mine / Others filter partitions without revealing owners',
     async ({ browser }) => {
  const alice = await operator(browser, {
    username: 'alice',
    permissions: [...BASE, 'broadcast.active_view'], activeSessions: THREE,
  });
  await alice.page.goto('/active-broadcasts');

  await alice.page.getByTestId('active-owner-mine').click();
  await expect(alice.page.getByTestId('active-row-31')).toBeVisible();
  await expect(alice.page.getByTestId('active-row-32')).toHaveCount(0);

  await alice.page.getByTestId('active-owner-others').click();
  await expect(alice.page.getByTestId('active-row-31')).toHaveCount(0);
  await expect(alice.page.getByTestId('active-row-32')).toBeVisible();
  // Still no identity for the others.
  await expect(alice.page.getByTestId('col-broadcaster')).toHaveCount(0);

  await alice.context.close();
});

// ===========================================================================
// SCENARIO 6 - Emergency Stop stays a different thing
// ===========================================================================
test('Emergency Stop All remains separate from the per-session Stop',
     async ({ browser }) => {
  const d = await operator(browser, {
    permissions: [...BASE, 'broadcast.active_view', 'broadcast.stop_any',
                  'broadcast.emergency_stop'],
    activeSessions: THREE,
  });

  // The per-session Stop lives on the supervision page...
  await d.page.goto('/active-broadcasts');
  await expect(d.page.getByTestId('active-stop-32')).toBeVisible();

  // ...and Emergency Stop All stays on the Console, with its own wording.
  await d.page.goto('/console');
  await expect(d.page.getByTestId('emergency-stop-btn')).toBeVisible();
  await d.page.getByTestId('emergency-stop-btn').click();
  await expect(d.page.getByTestId('emergency-confirm-modal'))
    .toContainText(/every active EchoCast broadcast/i);

  await d.context.close();
});

test('stop_any alone does not put an Emergency Stop button on the Console',
     async ({ browser }) => {
  const d = await operator(browser, {
    permissions: [...BASE, 'broadcast.active_view', 'broadcast.stop_any'],
    activeSessions: THREE,
  });
  await d.page.goto('/console');

  await expect(d.page.getByTestId('emergency-stop-btn')).toHaveCount(0);
  await d.context.close();
});
