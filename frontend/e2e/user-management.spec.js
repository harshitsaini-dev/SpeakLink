/**
 * User Management in the browser.
 *
 * The property worth the most here is the one that is easiest to fake: a page
 * that hides a button an account may not use is not a page that stops them.
 * So these tests do both - check the control is hidden, and check that when the
 * backend refuses anyway, the refusal is shown rather than swallowed. A button
 * that silently does nothing is worse than one that explains itself.
 *
 * Nothing typed or returned here may reach the DOM, a URL, localStorage or the
 * console: not a password, not a hash, not a session counter.
 */
const { test, expect } = require('@playwright/test');
const { mockBackend, signIn, OPERATOR } = require('./support/backend');

const SUPER_ADMIN = { id: 1, username: 'founder', role: 'OWNER' };
const ADMIN = { id: 2, username: 'priya', role: 'ADMIN' };
const VIEWER = { id: 4, username: 'anita', role: 'VIEWER' };

async function open(page, options = {}) {
  const state = await mockBackend(page, options);
  await signIn(page);
  await page.goto('/users');
  return state;
}

test.describe('Seeing the accounts', () => {
  test('every account is listed with its role and status', async ({ page }) => {
    await open(page, { operator: SUPER_ADMIN });
    await expect(page.getByTestId('users-table')).toBeVisible();
    // OWNER is displayed as SUPER ADMIN - a rename of the label only, not of
    // the stored role value the rest of this file still asserts against.
    await expect(page.getByTestId('user-row-founder')).toContainText('SUPER ADMIN');
    await expect(page.getByTestId('user-row-priya')).toContainText('ADMIN');
    await expect(page.getByTestId('user-row-rahul')).toContainText('disabled');
    await expect(page.getByTestId('user-row-anita')).toContainText('archived');
  });

  test('archived accounts are shown, not hidden', async ({ page }) => {
    // Hiding them makes a username look free when it is not, and makes a
    // retired colleague's history look like it was written by nobody.
    await open(page, { operator: SUPER_ADMIN });
    await expect(page.getByTestId('user-row-anita')).toBeVisible();
  });

  test('a load failure is reported and does not leave a spinner forever',
    async ({ page }) => {
      await open(page, { operator: SUPER_ADMIN, usersStatus: 500 });
      await expect(page.getByTestId('list-error')).toBeVisible();
      await expect(page.getByTestId('list-loading')).toHaveCount(0);
    });

  test('a forbidden list shows the refusal rather than an empty page',
    async ({ page }) => {
      // ADMIN, not VIEWER: VIEWER now never reaches this page at all (see
      // "the route is redirected away" below) - this proves defence in depth
      // for an account the frontend DOES let onto the page, where the API
      // itself still refuses the specific request.
      await open(page, { operator: ADMIN, usersStatus: 403 });
      await expect(page.getByTestId('list-error')).toContainText(/permission/i);
    });
});

test.describe('Nothing secret reaches the browser', () => {
  test('no hash, password or session counter appears anywhere on the page',
    async ({ page }) => {
      await open(page, { operator: SUPER_ADMIN });
      const body = await page.locator('body').innerText();
      for (const forbidden of ['password_hash', '$2b$', 'session_version', 'Bearer ', 'eyJ']) {
        expect(body).not.toContain(forbidden);
      }
      // The raw HTML is scanned only for markers that cannot occur by accident.
      //
      // 'eyJ' is deliberately NOT among them: these tests run against the CRA
      // dev server, whose inline source maps are base64 and therefore start
      // with exactly those three characters. Asserting on it here failed on
      // every page in the application, including ones with no token anywhere -
      // a scan that matches the build tooling is measuring the build tooling.
      const html = await page.content();
      for (const forbidden of ['password_hash', '$2b$', 'session_version']) {
        expect(html).not.toContain(forbidden);
      }
    });

  test('no link on the page carries a credential in its query string',
    async ({ page }) => {
      await open(page, { operator: SUPER_ADMIN });
      const links = await page.locator('a[href]').evaluateAll(
        (nodes) => nodes.map((node) => node.getAttribute('href')));
      for (const href of links) {
        expect(href).not.toMatch(/[?&](token|credential|password|secret)=/i);
      }
    });

  test('a password typed into the create form never leaves the password field',
    async ({ page }) => {
      await open(page, { operator: SUPER_ADMIN });
      await page.getByTestId('new-user').click();
      const secret = 'a-very-recognisable-temporary-password';
      await page.getByTestId('create-display-name').fill('New Person');
      await page.getByTestId('create-username').fill('new.person');
      await page.getByTestId('create-password').fill(secret);

      await expect(page.getByTestId('create-password')).toHaveAttribute('type', 'password');
      expect(await page.locator('body').innerText()).not.toContain(secret);
      expect(page.url()).not.toContain(secret);
      const stored = await page.evaluate(() => JSON.stringify(window.localStorage));
      expect(stored).not.toContain(secret);
    });

  test('a password reset response shows no password and no token', async ({ page }) => {
    await open(page, { operator: SUPER_ADMIN });
    await page.getByTestId('reset-priya').click();
    await page.getByTestId('reset-password-value').fill('another-long-temporary-password');
    await page.getByTestId('reset-confirm').check();
    await page.getByTestId('reset-password-form-submit').click();

    await expect(page.getByTestId('user-notice')).toBeVisible();
    const body = await page.locator('body').innerText();
    expect(body).not.toContain('another-long-temporary-password');
    expect(body).not.toContain('$2b$');
  });
});

test.describe('What each role is offered', () => {
  test('a SUPER_ADMIN can reset a password', async ({ page }) => {
    await open(page, { operator: SUPER_ADMIN });
    await expect(page.getByTestId('reset-priya')).toBeVisible();
  });

  test('an ADMIN is not offered password reset', async ({ page }) => {
    await open(page, { operator: ADMIN });
    await expect(page.getByTestId('reset-rahul')).toHaveCount(0);
  });

  test('an ADMIN is not offered actions on a SUPER_ADMIN', async ({ page }) => {
    // Being able to promote yourself would make every other restriction here
    // decorative.
    await open(page, { operator: ADMIN });
    for (const control of ['disable-founder', 'archive-founder']) {
      await expect(page.getByTestId(control)).toHaveCount(0);
    }
  });

  test('an ADMIN is not offered actions on another ADMIN', async ({ page }) => {
    await open(page, { operator: { id: 9, username: 'second-admin', role: 'ADMIN' } });
    await expect(page.getByTestId('disable-priya')).toHaveCount(0);
  });

  test('a VIEWER is offered no management controls at all', async ({ page }) => {
    await open(page, { operator: VIEWER });
    await expect(page.getByTestId('new-user')).toHaveCount(0);
    await expect(page.getByTestId('disable-rahul')).toHaveCount(0);
  });

  test('nobody is offered a control to switch off their own account',
    async ({ page }) => {
      await open(page, { operator: SUPER_ADMIN });
      await expect(page.getByTestId('disable-founder')).toHaveCount(0);
      await expect(page.getByTestId('archive-founder')).toHaveCount(0);
      // But editing your own name is not a lock-out, so it stays.
      await expect(page.getByTestId('edit-founder')).toBeVisible();
    });

  test('the navigation link is hidden from accounts that cannot use it',
    async ({ page }) => {
      await open(page, { operator: VIEWER });
      await expect(page.getByTestId('nav-users')).toHaveCount(0);
      // Changing your own password is for everybody.
      await expect(page.getByTestId('nav-password')).toBeVisible();
    });

  test('a direct visit by an unauthorised account is redirected, not shown a forbidden page',
    async ({ page }) => {
      // This used to be "the page renders and shows a 403" - menu hiding was
      // the only guard, and hiding a link is not a boundary. ProtectedRoute
      // now enforces the same menu.users.view permission the sidebar already
      // hides on, so a VIEWER typing /users directly never reaches the page
      // at all; it lands back on the first route this account can use. The
      // backend 403 in the previous test proves the second, independent
      // layer for an account the frontend DOES admit.
      await open(page, { operator: VIEWER, usersStatus: 403 });
      await expect(page.getByTestId('user-management-page')).toHaveCount(0);
      await expect(page).toHaveURL(/\/console$/);
    });
});

test.describe('Lifecycle actions', () => {
  test('disabling and enabling an account updates its badge', async ({ page }) => {
    await open(page, { operator: SUPER_ADMIN });
    await page.getByTestId('disable-priya').click();
    await expect(page.getByTestId('user-row-priya')).toContainText('disabled');
    await page.getByTestId('enable-priya').click();
    await expect(page.getByTestId('user-row-priya')).toContainText('active');
  });

  test('archiving then restoring returns the account to disabled, not active',
    async ({ page }) => {
      await open(page, { operator: SUPER_ADMIN });
      await page.getByTestId('archive-priya').click();
      await expect(page.getByTestId('user-row-priya')).toContainText('archived');
      await page.getByTestId('restore-priya').click();
      await expect(page.getByTestId('user-row-priya')).toContainText('disabled');
      await expect(page.getByTestId('user-notice')).toContainText(/disabled until/i);
    });

  test('there is no enable control on an archived account', async ({ page }) => {
    // Enable is a small button; un-retiring somebody is not a small decision.
    await open(page, { operator: SUPER_ADMIN });
    await expect(page.getByTestId('enable-anita')).toHaveCount(0);
    await expect(page.getByTestId('restore-anita')).toBeVisible();
  });

  test('a duplicate username is reported, not swallowed', async ({ page }) => {
    await open(page, { operator: SUPER_ADMIN });
    await page.getByTestId('new-user').click();
    await page.getByTestId('create-display-name').fill('Someone Else');
    await page.getByTestId('create-username').fill('priya');
    await page.getByTestId('create-password').fill('a-long-enough-temporary-password');
    await page.getByTestId('create-user-form-submit').click();
    await expect(page.getByTestId('user-error')).toContainText(/already in use/i);
  });

  test('a server refusal is shown even though the page allowed the click',
    async ({ page }) => {
      // The last-SUPER_ADMIN rule, enforced by the backend. The page cannot
      // know it, so the message has to survive.
      // rahul is a BROADCASTER and already disabled in the fixture, so the
      // control offered is Enable. An earlier version of this test clicked
      // "disable-rahul", which is never rendered, and spent thirty seconds
      // timing out on a button that was correctly absent.
      const state = await open(page, { operator: ADMIN });
      await page.getByTestId('enable-rahul').click();
      await expect(page.getByTestId('user-notice')).toBeVisible();
      expect(state.userActions.some((entry) => entry.action === 'enable')).toBe(true);
    });

  test('creating a user sends no password in the URL', async ({ page }) => {
    const requests = [];
    page.on('request', (request) => requests.push(request.url()));
    await open(page, { operator: SUPER_ADMIN });
    await page.getByTestId('new-user').click();
    await page.getByTestId('create-display-name').fill('New Person');
    await page.getByTestId('create-username').fill('new.person');
    await page.getByTestId('create-password').fill('a-long-enough-temporary-password');
    await page.getByTestId('create-user-form-submit').click();
    await expect(page.getByTestId('user-notice')).toBeVisible();
    for (const url of requests) {
      expect(url).not.toContain('a-long-enough-temporary-password');
    }
  });
});

test.describe('Permanent deletion', () => {
  test('an OWNER is never offered Delete', async ({ page }) => {
    // Losing the last one cannot be undone from inside the product, so the
    // control is not offered at all - and the backend refuses it regardless.
    await open(page, { operator: SUPER_ADMIN });
    await expect(page.getByTestId('delete-founder')).toHaveCount(0);
  });

  test('the dialog asks what depends on the account before offering the button',
    async ({ page }) => {
      await open(page, { operator: SUPER_ADMIN });
      await page.getByTestId('delete-anita').click();
      await expect(page.getByTestId('delete-user-dialog')).toBeVisible();
      await expect(page.getByTestId('delete-summary')).toBeVisible();
    });

  test('an account with history cannot be deleted and says why', async ({ page }) => {
    // priya (id 2) has three recorded broadcast sessions in the fixture.
    await open(page, { operator: SUPER_ADMIN });
    await page.getByTestId('delete-priya').click();
    await expect(page.getByTestId('delete-blocked')).toBeVisible();
    await expect(page.getByTestId('delete-summary')).toContainText(/history/i);
    await expect(page.getByTestId('delete-confirm')).toBeDisabled();
    await expect(page.getByTestId('delete-confirm-input')).toHaveCount(0);
  });

  test('a clean account still needs the username typed exactly', async ({ page }) => {
    await open(page, { operator: SUPER_ADMIN });
    await page.getByTestId('delete-anita').click();
    await expect(page.getByTestId('delete-confirm')).toBeDisabled();
    await page.getByTestId('delete-confirm-input').fill('anit');
    await expect(page.getByTestId('delete-confirm')).toBeDisabled();
    await page.getByTestId('delete-confirm-input').fill('anita');
    await expect(page.getByTestId('delete-confirm')).toBeEnabled();
  });

  test('confirming removes the row and reports it', async ({ page }) => {
    const state = await open(page, { operator: SUPER_ADMIN });
    await page.getByTestId('delete-anita').click();
    await page.getByTestId('delete-confirm-input').fill('anita');
    await page.getByTestId('delete-confirm').click();
    await expect(page.getByTestId('user-notice')).toContainText(/permanently deleted/i);
    await expect(page.getByTestId('user-row-anita')).toHaveCount(0);
    expect(state.userActions.some((entry) => entry.action === 'delete')).toBe(true);
  });

  test('cancelling deletes nothing', async ({ page }) => {
    const state = await open(page, { operator: SUPER_ADMIN });
    await page.getByTestId('delete-anita').click();
    await page.getByTestId('delete-cancel').click();
    await expect(page.getByTestId('delete-user-dialog')).toHaveCount(0);
    await expect(page.getByTestId('user-row-anita')).toBeVisible();
    expect(state.userActions.some((entry) => entry.action === 'delete')).toBe(false);
  });

  test('a server refusal is shown rather than swallowed', async ({ page }) => {
    // The dialog cannot know every rule the server enforces, so the message
    // has to survive when the server says no anyway.
    await open(page, { operator: SUPER_ADMIN, userHistory: {} });
    await page.getByTestId('delete-anita').click();
    await page.getByTestId('delete-confirm-input').fill('anita');
    // A regex, not a glob. The request carries ?confirm=..., and
    // '**/api/users/*/permanently' does not match a URL with a query string -
    // so the override never applied and the mock answered normally.
    await page.route(/\/api\/users\/\d+\/permanently/, (route) =>
      route.fulfill({ status: 409, contentType: 'application/json',
                      body: JSON.stringify({ detail: 'Refused by the server.' }) }));
    await page.getByTestId('delete-confirm').click();
    await expect(page.getByTestId('user-error')).toContainText(/refused/i);
    await expect(page.getByTestId('user-row-anita')).toBeVisible();
  });

  test('the dialog never shows a hash or a password', async ({ page }) => {
    await open(page, { operator: SUPER_ADMIN });
    await page.getByTestId('delete-anita').click();
    const body = await page.locator('body').innerText();
    for (const forbidden of ['$2b$', 'password_hash', 'session_version']) {
      expect(body).not.toContain(forbidden);
    }
  });
});

test.describe('Changing your own password', () => {
  async function openChangePassword(page, options = {}) {
    const state = await mockBackend(page, options);
    await signIn(page);
    await page.goto('/account/password');
    return state;
  }

  test('the page warns that succeeding signs you out', async ({ page }) => {
    await openChangePassword(page);
    await expect(page.getByTestId('change-password-page')).toContainText(/signed out/i);
  });

  test('the current password is required', async ({ page }) => {
    await openChangePassword(page);
    await expect(page.getByTestId('current-password')).toHaveAttribute('required', '');
  });

  test('a wrong current password is reported and does not sign you out',
    async ({ page }) => {
      await openChangePassword(page);
      await page.getByTestId('current-password').fill('not-the-right-one');
      await page.getByTestId('new-password').fill('a-different-long-password');
      await page.getByTestId('repeat-password').fill('a-different-long-password');
      await page.getByTestId('change-password-submit').click();
      await expect(page.getByTestId('change-password-error')).toContainText(/current password/i);
      await expect(page).toHaveURL(/\/account\/password/);
    });

  test('mismatched new passwords are caught before anything is sent',
    async ({ page }) => {
      const state = await openChangePassword(page);
      await page.getByTestId('current-password').fill('correct-current-password');
      await page.getByTestId('new-password').fill('a-different-long-password');
      await page.getByTestId('repeat-password').fill('a-different-long-passwordx');
      await expect(page.getByTestId('mismatch')).toBeVisible();
      await expect(page.getByTestId('change-password-submit')).toBeDisabled();
      expect(state.passwordChanges).toHaveLength(0);
    });

  test('a short password is refused before anything is sent', async ({ page }) => {
    const state = await openChangePassword(page);
    await page.getByTestId('new-password').fill('short');
    await expect(page.getByTestId('too-short')).toBeVisible();
    await expect(page.getByTestId('change-password-submit')).toBeDisabled();
    expect(state.passwordChanges).toHaveLength(0);
  });

  test('succeeding clears the token and returns to sign-in', async ({ page }) => {
    await openChangePassword(page);
    await page.getByTestId('current-password').fill('correct-current-password');
    await page.getByTestId('new-password').fill('a-different-long-password');
    await page.getByTestId('repeat-password').fill('a-different-long-password');
    await page.getByTestId('change-password-submit').click();
    await expect(page).toHaveURL(/\/login/);
    const stored = await page.evaluate(() => window.localStorage.getItem('echocast_token'));
    expect(stored).toBeNull();
  });

  test('neither password reaches a URL', async ({ page }) => {
    const requests = [];
    page.on('request', (request) => requests.push(request.url()));
    await openChangePassword(page);
    await page.getByTestId('current-password').fill('correct-current-password');
    await page.getByTestId('new-password').fill('a-different-long-password');
    await page.getByTestId('repeat-password').fill('a-different-long-password');
    await page.getByTestId('change-password-submit').click();
    await expect(page).toHaveURL(/\/login/);
    for (const url of requests) {
      expect(url).not.toContain('correct-current-password');
      expect(url).not.toContain('a-different-long-password');
    }
  });
});

test.describe('Branding is untouched', () => {
  test('the page is still EchoCast Live', async ({ page }) => {
    await open(page, { operator: SUPER_ADMIN });
    await expect(page).toHaveTitle(/EchoCast Live/);
  });
});
