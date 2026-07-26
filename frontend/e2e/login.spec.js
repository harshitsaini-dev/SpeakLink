const { test, expect } = require('@playwright/test');
const { mockBackend } = require('./support/backend');

// ---------------------------------------------------------------------------
// The login page must never hand out a credential
//
// The form shipped with a username and password already typed into it, and
// printed the same pair as a "Default:" hint under the Sign In button. Anyone
// who could reach the page could read a working credential and sign in without
// knowing anything. Pre-filling is the same problem wearing a convenience
// costume: the operator never has to know their own password, so it never gets
// changed from the default.
//
// The historical value is assembled from parts below rather than written out,
// so the repository holds no copy-pasteable credential while the assertion that
// guards against its return still works.
const HISTORICAL_DEFAULT_PASSWORD = 'admin' + '123';
// ---------------------------------------------------------------------------
test.describe('The login form reveals no credential', () => {
  test.beforeEach(async ({ page }) => {
    await mockBackend(page);
    await page.goto('/login');
  });

  test('the username field starts empty', async ({ page }) => {
    await expect(page.getByTestId('login-username-input')).toHaveValue('');
  });

  test('the password field starts empty', async ({ page }) => {
    await expect(page.getByTestId('login-password-input')).toHaveValue('');
  });

  test('the default password appears nowhere on the page', async ({ page }) => {
    const body = (await page.textContent('body')) || '';
    const html = await page.content();
    expect(body).not.toContain(HISTORICAL_DEFAULT_PASSWORD);
    expect(html).not.toContain(HISTORICAL_DEFAULT_PASSWORD);
  });

  test('no credential hint is displayed', async ({ page }) => {
    // "HQ Admin Sign In" is a legitimate heading, so a bare search for "admin"
    // would fail for the wrong reason. What must be absent is a hint that
    // *supplies* a credential: a "Default:" line, or a user / pass pair.
    const body = (await page.textContent('body')) || '';
    expect(body).not.toMatch(/default\s*[:–-]/i);
    expect(body).not.toMatch(/\badmin\s*\/\s*\S+/i);
    expect(body).not.toMatch(/\b(password|credential)s?\s*[:=]\s*\S+/i);
  });

  test('the operator is not told which username to use', async ({ page }) => {
    const username = page.getByTestId('login-username-input');
    await expect(username).toHaveValue('');
    // A placeholder is just as much of a hint as a value.
    const placeholder = await username.getAttribute('placeholder');
    expect((placeholder || '').toLowerCase()).not.toContain('admin');
  });

  test('the password field is a password field', async ({ page }) => {
    await expect(page.getByTestId('login-password-input')).toHaveAttribute('type', 'password');
  });

  test('the fields carry sensible autocomplete values', async ({ page }) => {
    await expect(page.getByTestId('login-username-input')).toHaveAttribute('autocomplete', 'username');
    await expect(page.getByTestId('login-password-input')).toHaveAttribute('autocomplete', 'current-password');
  });
});

test.describe('HQ sign in', () => {
  test('a valid operator reaches the Broadcast Console', async ({ page }) => {
    await mockBackend(page);
    await page.goto('/login');

    await page.getByTestId('login-username-input').fill('pilot-operator');
    await page.getByTestId('login-password-input').fill('not-a-real-password');
    await page.getByTestId('login-submit-btn').click();

    await expect(page.getByTestId('broadcast-console')).toBeVisible();
    await expect(page).toHaveURL(/\/console$/);
  });

  test('a rejected sign in shows the error and stays on the login page', async ({ page }) => {
    await mockBackend(page, { loginStatus: 401 });
    await page.goto('/login');

    await page.getByTestId('login-username-input').fill('pilot-operator');
    await page.getByTestId('login-password-input').fill('wrong');
    await page.getByTestId('login-submit-btn').click();

    await expect(page.getByTestId('login-error')).toBeVisible();
    await expect(page).toHaveURL(/\/login$/);
    await expect(page.getByTestId('broadcast-console')).toHaveCount(0);
  });

  test('a throttled or locked sign in shows a neutral retry message', async ({ page }) => {
    // The backend answers 429 identically for "too fast" and "this account is
    // temporarily locked", because distinguishing them would reveal whether
    // the account exists.
    await mockBackend(page, { loginStatus: 429 });
    await page.goto('/login');
    await page.getByTestId('login-username-input').fill('pilot-operator');
    await page.getByTestId('login-password-input').fill('not-a-real-password');
    await page.getByTestId('login-submit-btn').click();

    const error = page.getByTestId('login-error');
    await expect(error).toBeVisible();
    await expect(error).toContainText(/try again/i);

    const message = (await error.textContent()) || '';
    expect(message).not.toMatch(/\d/);            // no count, threshold or unlock time
    expect(message.toLowerCase()).not.toContain('lock');
    expect(message.toLowerCase()).not.toContain('exist');
  });

  test('a throttled sign in stores no token and stays on the login page', async ({ page }) => {
    await mockBackend(page, { loginStatus: 429 });
    await page.goto('/login');
    await page.getByTestId('login-username-input').fill('pilot-operator');
    await page.getByTestId('login-password-input').fill('not-a-real-password');
    await page.getByTestId('login-submit-btn').click();
    await expect(page.getByTestId('login-error')).toBeVisible();

    expect(await page.evaluate(() => window.localStorage.getItem('echocast_token'))).toBeNull();
    await expect(page).toHaveURL(/\/login$/);
  });

  test('the Sign In button leaves its loading state after a refusal', async ({ page }) => {
    await mockBackend(page, { loginStatus: 429 });
    await page.goto('/login');
    await page.getByTestId('login-username-input').fill('pilot-operator');
    await page.getByTestId('login-password-input').fill('not-a-real-password');
    await page.getByTestId('login-submit-btn').click();
    await expect(page.getByTestId('login-error')).toBeVisible();

    const button = page.getByTestId('login-submit-btn');
    await expect(button).toBeEnabled();
    await expect(button).toContainText(/sign in/i);
  });

  test('a rejected sign in stores no token', async ({ page }) => {
    await mockBackend(page, { loginStatus: 401 });
    await page.goto('/login');
    await page.getByTestId('login-username-input').fill('pilot-operator');
    await page.getByTestId('login-password-input').fill('wrong');
    await page.getByTestId('login-submit-btn').click();
    await expect(page.getByTestId('login-error')).toBeVisible();

    const token = await page.evaluate(() => window.localStorage.getItem('echocast_token'));
    expect(token).toBeNull();
  });
});
