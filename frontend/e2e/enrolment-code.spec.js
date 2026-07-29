/**
 * The one-time enrolment code: countdown, state, and disappearing when dead.
 *
 * A code is short-lived and shown once. "Expires in 15 minutes", written at the
 * moment of issue, is wrong from the second after - an operator reading it ten
 * minutes later has no idea how long they have. So the panel counts down, and
 * when it reaches zero it removes the value from the DOM entirely: a dead
 * secret left on screen is still a secret on screen.
 *
 * UNUSED and EXPIRED are the only states this page can honestly report from the
 * clock alone. It does not claim USED, because nothing tells it when a code was
 * redeemed.
 */
const { test, expect } = require('@playwright/test');
const { mockBackend, signIn } = require('./support/backend');

async function openDevices(page, options = {}) {
  const state = await mockBackend(page, options);
  await signIn(page);
  await page.goto('/stores/1/devices');
  await expect(page.getByTestId('receiver-devices-page')).toBeVisible();
  return state;
}

test.describe('Issuing a code', () => {
  test('the panel appears with a countdown and an UNUSED state', async ({ page }) => {
    await openDevices(page);
    await page.getByTestId('create-enrolment-code-btn').click();
    await expect(page.getByTestId('enrolment-code-panel')).toBeVisible();
    await expect(page.getByTestId('enrolment-code-state')).toHaveText('UNUSED');
    await expect(page.getByTestId('enrolment-code-countdown')).toContainText(/expires in \d+:\d{2}/);
  });

  test('the countdown actually counts down', async ({ page }) => {
    await openDevices(page);
    await page.getByTestId('create-enrolment-code-btn').click();
    const first = await page.getByTestId('enrolment-code-countdown').innerText();
    await page.waitForTimeout(2200);
    const later = await page.getByTestId('enrolment-code-countdown').innerText();
    expect(later).not.toBe(first);
  });

  test('the countdown is announced to assistive technology', async ({ page }) => {
    await openDevices(page);
    await page.getByTestId('create-enrolment-code-btn').click();
    await expect(page.getByTestId('enrolment-code-countdown'))
      .toHaveAttribute('aria-live', 'polite');
  });

  test('the code itself is shown while it is still usable', async ({ page }) => {
    await openDevices(page);
    await page.getByTestId('create-enrolment-code-btn').click();
    await expect(page.getByTestId('issued-enrolment-code-value')).toBeVisible();
  });
});

test.describe('When the code runs out', () => {
  // A two-second life, so the test watches the real transition rather than
  // mocking the clock and proving nothing about the component.
  const SHORT = { enrolmentCodeSeconds: 2 };

  test('the state becomes EXPIRED', async ({ page }) => {
    await openDevices(page, SHORT);
    await page.getByTestId('create-enrolment-code-btn').click();
    await expect(page.getByTestId('enrolment-code-state')).toHaveText('EXPIRED',
      { timeout: 8000 });
  });

  test('the value is removed from the page entirely', async ({ page }) => {
    await openDevices(page, SHORT);
    await page.getByTestId('create-enrolment-code-btn').click();
    const code = await page.getByTestId('issued-enrolment-code-value').innerText();
    await expect(page.getByTestId('enrolment-code-expired')).toBeVisible({ timeout: 8000 });
    await expect(page.getByTestId('issued-enrolment-code-value')).toHaveCount(0);
    expect(await page.content()).not.toContain(code);
  });

  test('the countdown stops rather than going negative', async ({ page }) => {
    await openDevices(page, SHORT);
    await page.getByTestId('create-enrolment-code-btn').click();
    await expect(page.getByTestId('enrolment-code-expired')).toBeVisible({ timeout: 8000 });
    await expect(page.getByTestId('enrolment-code-countdown')).toHaveCount(0);
  });

  test('it explains what to do next', async ({ page }) => {
    await openDevices(page, SHORT);
    await page.getByTestId('create-enrolment-code-btn').click();
    await expect(page.getByTestId('enrolment-code-expired'))
      .toContainText(/generate a new one/i, { timeout: 8000 });
  });

  test('it can be dismissed', async ({ page }) => {
    await openDevices(page, SHORT);
    await page.getByTestId('create-enrolment-code-btn').click();
    await expect(page.getByTestId('enrolment-code-dismiss')).toBeVisible({ timeout: 8000 });
    await page.getByTestId('enrolment-code-dismiss').click();
    await expect(page.getByTestId('enrolment-code-panel')).toHaveCount(0);
  });
});

test.describe('Nothing leaks', () => {
  test('the code never reaches a URL', async ({ page }) => {
    const requests = [];
    page.on('request', (r) => requests.push(r.url()));
    await openDevices(page);
    await page.getByTestId('create-enrolment-code-btn').click();
    const code = await page.getByTestId('issued-enrolment-code-value').innerText();
    expect(page.url()).not.toContain(code);
    for (const url of requests) expect(url).not.toContain(code);
  });

  test('no Device credential is shown in the list', async ({ page }) => {
    await openDevices(page);
    const body = await page.locator('body').innerText();
    expect(body).not.toContain('speaklink_rcv_v1');
    expect(body).not.toContain('receiver_token');
  });
});
