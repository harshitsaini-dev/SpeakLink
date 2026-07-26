const { test, expect } = require('@playwright/test');
const { mockBackend } = require('./support/backend');

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
