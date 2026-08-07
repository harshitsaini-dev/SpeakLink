/**
 * Several operators on air at once, in a real browser.
 *
 * WHAT THIS PROVES THAT UNIT TESTS CANNOT
 *
 * That the console a person actually looks at shows the right thing: a Store
 * another broadcast holds is visibly unavailable, an ordinary Broadcaster is
 * never shown whose broadcast it is, and Emergency Stop asks a question that
 * says out loud it affects everyone.
 *
 * SEPARATE BROWSER CONTEXTS
 *
 * Each operator gets their own context, so each has its own storage and its
 * own signed-in identity - two `page`s in one context would share a token and
 * would not be two operators at all.
 *
 * WHAT IS DELIBERATELY NOT ASSERTED
 *
 * That any speaker fell silent. These tests drive commands and read screens;
 * acoustic proof is not something a browser can give.
 */
const { test, expect } = require('@playwright/test');
const { mockBackend, signIn } = require('./support/backend');

const ALICE_SESSION = {
  session_id: 31, campaign_name: 'Alice Diwali Offers',
  owner_user_id: 11, owner_username: 'alice', owner_display_name: 'Alice Kumar',
  started_at: '2026-08-03T10:00:00+00:00', target_store_ids: [1],
};
const BOB_SESSION = {
  session_id: 32, campaign_name: 'Bob Evening Reminder',
  owner_user_id: 22, owner_username: 'bob', owner_display_name: 'Bob Singh',
  started_at: '2026-08-03T10:05:00+00:00', target_store_ids: [2],
};

/** One signed-in operator with their own browser context. */
async function operator(browser, { username, role, permissions, activeSessions }) {
  const context = await browser.newContext();
  const page = await context.newPage();
  await signIn(page);
  await mockBackend(page, {
    operator: { id: 99, username, role },
    ...(permissions ? { permissions } : {}),
    activeSessions: activeSessions || [],
  });
  return { context, page };
}

// ===========================================================================
// SCENARIO 1 - another operator being live does not lock me out
// ===========================================================================
test('a Broadcaster can still broadcast while another operator is live',
     async ({ browser }) => {
  const bob = await operator(browser, {
    username: 'bob', role: 'BROADCASTER', activeSessions: [ALICE_SESSION],
  });
  await bob.page.goto('/console');

  // UN (store id 1) is Alice's target and must read as unavailable.
  await expect(bob.page.getByTestId('store-busy-UN')).toBeVisible();
  await expect(bob.page.getByTestId('store-checkbox-UN')).toBeDisabled();

  // But Bob is NOT locked out: a free Store is still selectable and Start is
  // still offered. This is the whole point of the change.
  await expect(bob.page.getByTestId('store-checkbox-ASR')).toBeEnabled();
  await expect(bob.page.getByTestId('start-broadcast-btn')).toBeVisible();

  await bob.context.close();
});

test('a Broadcaster is never told whose broadcast is using a Store',
     async ({ browser }) => {
  const bob = await operator(browser, {
    username: 'bob', role: 'BROADCASTER', activeSessions: [ALICE_SESSION],
  });
  await bob.page.goto('/console');
  await expect(bob.page.getByTestId('store-busy-UN')).toBeVisible();

  const rendered = (await bob.page.locator('body').innerText()).toLowerCase();
  for (const leak of ['alice', 'diwali', 'alice kumar', '31']) {
    expect(rendered.includes(leak),
           `"${leak}" leaked onto a Broadcaster's screen`).toBe(false);
  }
  // And no privileged panel at all - not even an anonymised one.
  await expect(bob.page.getByTestId('active-broadcasts-panel')).toHaveCount(0);

  await bob.context.close();
});

// ===========================================================================
// SCENARIO 3 - my own broadcast, and stopping only it
// ===========================================================================
test('an operator sees their own broadcast and only their own', async ({ browser }) => {
  const alice = await operator(browser, {
    username: 'alice', role: 'BROADCASTER',
    activeSessions: [ALICE_SESSION, BOB_SESSION],
  });
  await alice.page.goto('/console');

  await expect(alice.page.getByTestId('my-active-broadcast')).toBeVisible();
  await expect(alice.page.getByTestId('my-active-campaign'))
    .toHaveText('Alice Diwali Offers');
  // Bob's broadcast is invisible to her: she is a Broadcaster.
  const rendered = (await alice.page.locator('body').innerText()).toLowerCase();
  expect(rendered.includes('bob')).toBe(false);
  expect(rendered.includes('evening reminder')).toBe(false);

  await alice.context.close();
});

test("a Store my own broadcast holds is not marked busy to me", async ({ browser }) => {
  const alice = await operator(browser, {
    username: 'alice', role: 'BROADCASTER', activeSessions: [ALICE_SESSION],
  });
  await alice.page.goto('/console');

  await expect(alice.page.getByTestId('my-active-broadcast')).toBeVisible();
  await expect(alice.page.getByTestId('store-busy-UN')).toHaveCount(0);

  await alice.context.close();
});

// ===========================================================================
// SCENARIO 4 - Broadcaster permissions
// ===========================================================================
test('a Broadcaster has no Emergency Stop control', async ({ browser }) => {
  const bob = await operator(browser, { username: 'bob', role: 'BROADCASTER' });
  await bob.page.goto('/console');

  await expect(bob.page.getByTestId('emergency-stop-btn')).toHaveCount(0);
  await bob.context.close();
});

// ===========================================================================
// SCENARIO 5 - the privileged view
// ===========================================================================
// The cross-user list these tests used to assert has MOVED to
// /active-broadcasts - see active-broadcasts.spec.js, which proves the
// supervision page itself. What belongs here is the Console's own behaviour:
// a compact badge and nothing more, whatever else is on air.
test('an Admin gets a compact badge on the Console, not a list of broadcasts',
     async ({ browser }) => {
  const admin = await operator(browser, {
    username: 'priya', role: 'ADMIN',
    activeSessions: [ALICE_SESSION, BOB_SESSION],
  });
  await admin.page.goto('/console');

  await expect(admin.page.getByTestId('active-broadcasts-badge')).toBeVisible();
  await expect(admin.page.getByTestId('active-broadcasts-count')).toHaveText('2');

  // The old panel and its rows are gone.
  await expect(admin.page.getByTestId('active-broadcasts-panel')).toHaveCount(0);
  await expect(admin.page.getByTestId('active-campaign-31')).toHaveCount(0);

  await admin.context.close();
});

test('the Console names no other operator, even for a privileged Admin',
     async ({ browser }) => {
  const admin = await operator(browser, {
    username: 'priya', role: 'ADMIN', activeSessions: [ALICE_SESSION, BOB_SESSION],
  });
  await admin.page.goto('/console');

  // Ownership visibility is real, but it belongs on the supervision page.
  // The Console is for broadcasting, and it stays that size.
  const body = await admin.page.textContent('body');
  expect(body).not.toContain('Alice Kumar');
  expect(body).not.toContain('Bob Evening Reminder');

  await admin.context.close();
});

test('the Console badge offers no Stop for somebody else\'s broadcast',
     async ({ browser }) => {
  const admin = await operator(browser, {
    username: 'priya', role: 'ADMIN', activeSessions: [ALICE_SESSION, BOB_SESSION],
  });
  await admin.page.goto('/console');

  const badge = admin.page.getByTestId('active-broadcasts-badge');
  await expect(badge).toBeVisible();
  // A link to the supervision page, never an action. Cross-owner termination
  // is a deliberate act performed there, with its own permission.
  await expect(badge.locator('button')).toHaveCount(0);
  await expect(badge.getByTestId('active-broadcasts-link')).toBeVisible();

  await admin.context.close();
});

// ===========================================================================
// SCENARIO 6 - Emergency Stop All
// ===========================================================================
test('Emergency Stop asks a question that says it affects everyone',
     async ({ browser }) => {
  const admin = await operator(browser, {
    username: 'priya', role: 'ADMIN', activeSessions: [ALICE_SESSION, BOB_SESSION],
  });
  await admin.page.goto('/console');

  await admin.page.getByTestId('emergency-stop-btn').click();
  const modal = admin.page.getByTestId('emergency-confirm-modal');
  await expect(modal).toBeVisible();
  await expect(modal).toContainText(/all active/i);
  await expect(modal).toContainText(/other operators/i);

  await admin.context.close();
});

test('confirming Emergency Stop clears every active broadcast', async ({ browser }) => {
  const admin = await operator(browser, {
    username: 'priya', role: 'ADMIN', activeSessions: [ALICE_SESSION, BOB_SESSION],
  });
  await admin.page.goto('/console');

  await admin.page.getByTestId('emergency-stop-btn').click();
  await admin.page.getByTestId('emergency-confirm-btn').click();

  await expect(admin.page.getByTestId('emergency-result')).toContainText('2');
  // The Stores are free again, so nothing reads as busy.
  await expect(admin.page.getByTestId('store-busy-UN')).toHaveCount(0);
  // And the badge falls to zero - the count comes from the same active-truth
  // source the supervision page reads.
  await expect(admin.page.getByTestId('active-broadcasts-count')).toHaveText('0');

  await admin.context.close();
});

test('cancelling Emergency Stop leaves every broadcast alone', async ({ browser }) => {
  const admin = await operator(browser, {
    username: 'priya', role: 'ADMIN', activeSessions: [ALICE_SESSION, BOB_SESSION],
  });
  await admin.page.goto('/console');

  await admin.page.getByTestId('emergency-stop-btn').click();
  await admin.page.getByTestId('emergency-cancel-btn').click();

  await expect(admin.page.getByTestId('emergency-confirm-modal')).toHaveCount(0);
  // Nothing was stopped: both broadcasts are still counted.
  await expect(admin.page.getByTestId('active-broadcasts-count')).toHaveText('2');
  await expect(admin.page.getByTestId('store-busy-UN')).toBeVisible();

  await admin.context.close();
});

test('a partial Emergency Stop failure is reported as a failure', async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await signIn(page);
  await mockBackend(page, {
    operator: { id: 99, username: 'priya', role: 'ADMIN' },
    activeSessions: [ALICE_SESSION, BOB_SESSION],
    emergencyStopIncomplete: true,
  });
  await page.goto('/console');

  await page.getByTestId('emergency-stop-btn').click();
  await page.getByTestId('emergency-confirm-btn').click();

  const result = page.getByTestId('emergency-result');
  await expect(result).toContainText(/still live/i);
  await expect(result).not.toContainText(/all broadcasts stopped/i);

  await context.close();
});

// ===========================================================================
// SCENARIO 7/8 - overrides
// ===========================================================================
test('an Admin denied Emergency Stop does not get the button', async ({ browser }) => {
  const admin = await operator(browser, {
    username: 'priya', role: 'ADMIN',
    permissions: ['menu.broadcast.view', 'broadcast.start', 'broadcast.stop',
                  'broadcast.store_delivery',
                  'broadcast.view_ownership', 'broadcast.active_view',
                  'menu.stores.view', 'menu.receivers.view', 'menu.history.view'],
    activeSessions: [ALICE_SESSION],
  });
  await admin.page.goto('/console');

  await expect(admin.page.getByTestId('emergency-stop-btn')).toHaveCount(0);
  // The other capabilities are unaffected - they are separate permissions,
  // and denying the estate-wide one narrows nothing else.
  await expect(admin.page.getByTestId('active-broadcasts-badge')).toBeVisible();
  await expect(admin.page.getByTestId('nav-active-broadcasts')).toBeVisible();

  await admin.context.close();
});

test('an Admin denied ownership view keeps busy markers but loses the details',
     async ({ browser }) => {
  const admin = await operator(browser, {
    username: 'priya', role: 'ADMIN',
    permissions: ['menu.broadcast.view', 'broadcast.start', 'broadcast.stop',
                  'broadcast.store_delivery',
                  'broadcast.emergency_stop', 'menu.stores.view',
                  'menu.receivers.view', 'menu.history.view'],
    activeSessions: [ALICE_SESSION],
  });
  await admin.page.goto('/console');

  await expect(admin.page.getByTestId('store-busy-UN')).toBeVisible();
  await expect(admin.page.getByTestId('active-broadcasts-panel')).toHaveCount(0);
  const rendered = (await admin.page.locator('body').innerText()).toLowerCase();
  expect(rendered.includes('alice')).toBe(false);
  // Emergency Stop is unaffected - again, separate capabilities.
  await expect(admin.page.getByTestId('emergency-stop-btn')).toBeVisible();

  await admin.context.close();
});

// ===========================================================================
// SCENARIO 10 - three operators at once
// ===========================================================================
test('three operators are live at once and each sees only their own',
     async ({ browser }) => {
  const CAROL_SESSION = {
    session_id: 33, campaign_name: 'Carol Weekend Sale',
    owner_user_id: 33, owner_username: 'carol', owner_display_name: 'Carol Das',
    started_at: '2026-08-03T10:10:00+00:00', target_store_ids: [5],
  };
  const all = [ALICE_SESSION, BOB_SESSION, CAROL_SESSION];

  const people = [];
  for (const username of ['alice', 'bob', 'carol']) {
    people.push(await operator(browser, {
      username, role: 'BROADCASTER', activeSessions: all,
    }));
  }

  const expected = {
    alice: 'Alice Diwali Offers',
    bob: 'Bob Evening Reminder',
    carol: 'Carol Weekend Sale',
  };
  const names = ['alice', 'bob', 'carol'];
  for (let index = 0; index < people.length; index += 1) {
    const { page } = people[index];
    const me = names[index];
    await page.goto('/console');
    await expect(page.getByTestId('my-active-campaign')).toHaveText(expected[me]);

    const rendered = (await page.locator('body').innerText()).toLowerCase();
    for (const other of names.filter((n) => n !== me)) {
      expect(rendered.includes(other),
             `${me} could see ${other}`).toBe(false);
    }
  }

  for (const person of people) await person.context.close();
});
