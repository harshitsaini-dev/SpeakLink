/**
 * Permanently deleting a Store, and being refused when history depends on it.
 *
 * A Store owns Receiver Devices, broadcast targets, sessions and Receiver
 * events. Removing the row would orphan that history or cascade it away, and
 * both destroy the only record of what was announced where. So deletion exists
 * for exactly one case - a Store created by mistake that nothing has ever
 * referenced - and everything else is pointed at Archive.
 *
 * The dialog asks the backend what depends on the Store before offering the
 * button. That is a courtesy, not the control: the server recounts inside the
 * transaction that deletes, so a Device enrolled a second ago still wins.
 */
const { test, expect } = require('@playwright/test');
const { mockBackend, signIn } = require('./support/backend');

async function open(page, options = {}) {
  const state = await mockBackend(page, options);
  await signIn(page);
  await page.goto('/stores');
  await expect(page.getByTestId('store-mgmt-row-UN')).toBeVisible();
  return state;
}

test.describe('Seeing what depends on a Store', () => {
  test('the dialog checks before it offers anything', async ({ page }) => {
    await open(page);
    await page.getByTestId('delete-store-ASR').click();
    await expect(page.getByTestId('delete-store-modal')).toBeVisible();
    await expect(page.getByTestId('delete-store-summary')).toBeVisible();
  });

  test('a Store with Receiver Devices cannot be deleted and says why',
    async ({ page }) => {
      await open(page);
      await page.getByTestId('delete-store-UN').click();
      await expect(page.getByTestId('delete-store-blocked')).toBeVisible();
      await expect(page.getByTestId('delete-store-summary')).toContainText(/history/i);
      await expect(page.getByTestId('delete-store-confirm')).toBeDisabled();
      await expect(page.getByTestId('delete-store-confirm-input')).toHaveCount(0);
    });

  test('the refusal points at Archive rather than just saying no',
    async ({ page }) => {
      await open(page);
      await page.getByTestId('delete-store-UN').click();
      await expect(page.getByTestId('delete-store-blocked')).toContainText(/archive/i);
    });
});

test.describe('Deleting a never-used Store', () => {
  test('the short code must be typed exactly', async ({ page }) => {
    await open(page);
    await page.getByTestId('delete-store-ASR').click();
    await expect(page.getByTestId('delete-store-confirm')).toBeDisabled();
    await page.getByTestId('delete-store-confirm-input').fill('asr');
    await expect(page.getByTestId('delete-store-confirm')).toBeDisabled();
    await page.getByTestId('delete-store-confirm-input').fill('ASR');
    await expect(page.getByTestId('delete-store-confirm')).toBeEnabled();
  });

  test('confirming removes the row', async ({ page }) => {
    const state = await open(page);
    await page.getByTestId('delete-store-ASR').click();
    await page.getByTestId('delete-store-confirm-input').fill('ASR');
    await page.getByTestId('delete-store-confirm').click();
    await expect(page.getByTestId('store-mgmt-row-ASR')).toHaveCount(0);
    expect(state.transitions.some((t) => t.action === 'delete')).toBe(true);
  });

  test('cancelling deletes nothing', async ({ page }) => {
    const state = await open(page);
    await page.getByTestId('delete-store-ASR').click();
    await page.getByTestId('delete-store-cancel').click();
    await expect(page.getByTestId('delete-store-modal')).toHaveCount(0);
    await expect(page.getByTestId('store-mgmt-row-ASR')).toBeVisible();
    expect(state.transitions.some((t) => t.action === 'delete')).toBe(false);
  });

  test('a server refusal is shown rather than swallowed', async ({ page }) => {
    // The dialog cannot know every rule the server enforces - a Device can be
    // enrolled between the check and the click - so the message has to survive.
    await open(page);
    await page.getByTestId('delete-store-ASR').click();
    await page.getByTestId('delete-store-confirm-input').fill('ASR');
    // A regex, not a glob: the request carries ?confirm=... and a glob ending
    // in the path does not match a URL with a query string.
    await page.route(/\/api\/stores\/\d+\/permanently/, (route) =>
      route.fulfill({ status: 409, contentType: 'application/json',
                      body: JSON.stringify({ detail: 'A Device was enrolled a moment ago.' }) }));
    await page.getByTestId('delete-store-confirm').click();
    await expect(page.getByTestId('delete-store-error')).toContainText(/enrolled/i);
    await expect(page.getByTestId('store-mgmt-row-ASR')).toBeVisible();
  });
});

test.describe('Nothing secret is shown', () => {
  test('the dialog reveals no credential or token', async ({ page }) => {
    await open(page);
    await page.getByTestId('delete-store-UN').click();
    const body = await page.locator('body').innerText();
    for (const forbidden of ['speaklink_rcv_v1', 'receiver_token', '$2b$', 'Bearer ']) {
      expect(body).not.toContain(forbidden);
    }
  });
});
