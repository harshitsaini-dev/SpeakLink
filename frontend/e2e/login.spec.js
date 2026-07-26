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
// The literal below appears only inside a negative assertion. It is the one
// place it is worth writing down, because a regression that reintroduces it is
// exactly what these tests exist to catch.
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
    expect(body).not.toContain('admin123');
    expect(html).not.toContain('admin123');
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

  test('a rejected sign in stores no token', async ({ page }) => {
    await mockBackend(page, { loginStatus: 401 });
    await page.goto('/login');
    await page.getByTestId('login-username-input').fill('pilot-operator');
    await page.getByTestId('login-password-input').fill('wrong');
    await page.getByTestId('login-submit-btn').click();
    await expect(page.getByTestId('login-error')).toBeVisible();

    const token = await page.evaluate(() => window.localStorage.getItem('speaklink_token'));
    expect(token).toBeNull();
  });
});
