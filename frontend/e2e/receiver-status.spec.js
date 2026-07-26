const { test, expect } = require('@playwright/test');
const { mockBackend, signIn } = require('./support/backend');

test.describe('Receiver Status', () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page);
  });

  // StatusBadge uppercases with CSS only, so the DOM text stays lowercase.
  // Matching case-insensitively keeps these tests about state, not styling.
  test('shows the connected Store as ONLINE and the rest as OFFLINE', async ({ page }) => {
    await mockBackend(page);
    await page.goto('/receivers');

    await expect(page.getByTestId('receivers-page')).toBeVisible();
    await expect(page.getByTestId('receiver-card-UN')).toContainText(/online/i);
    await expect(page.getByTestId('receiver-card-ASR')).toContainText(/offline/i);
    await expect(page.getByTestId('receiver-card-DM')).toContainText(/offline/i);
  });

  test('an offline Receiver is never rendered as online', async ({ page }) => {
    // The frontend was accused of a status mismatch during the amplifier pilot.
    // It was correct: the Receiver really had died. This pins that behaviour -
    // whatever the backend says is what is shown, with nothing inferred.
    await mockBackend(page, {
      stores: [
        { id: 1, store_code: 'UN', store_name: 'Uttam Nagar Old', city: 'UN ZONE', region: 'UN ZONE', is_online_store: true, is_active: true, status: 'offline' },
      ],
    });
    await page.goto('/receivers');

    const card = page.getByTestId('receiver-card-UN');
    await expect(card).toContainText(/offline/i);
    // "offline" does not contain "online", so this is a real assertion.
    await expect(card).not.toContainText(/\bonline\b/i);
  });

  test('Store UN is shown under its own zone with its real name', async ({ page }) => {
    await mockBackend(page);
    await page.goto('/receivers');

    const card = page.getByTestId('receiver-card-UN');
    await expect(card).toContainText('Uttam Nagar Old');
    // ASR is a different Store with a deliberately similar name.
    await expect(card).not.toContainText('ASR');
  });
});
