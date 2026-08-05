/**
 * Returning to Broadcast Console while the broadcast is still on air.
 *
 * The operator's report, reproduced in a real browser: start a broadcast, go
 * to another page, come back, and the Console shows an empty Campaign Name
 * with nothing selected - while the broadcast is still running and still
 * reaching those Stores.
 *
 * This drives real navigation rather than remounting a component, because the
 * router is what destroys the Console's local state. It also asserts the
 * negative half - that no second broadcast session was created - since a
 * "restore" that silently re-broadcast would satisfy every positive check.
 */
const { test, expect } = require('@playwright/test');
const {
  mockBackend, signIn, instrumentMicrophone, stubWebSocket,
} = require('./support/backend');

const CAMPAIGN = 'NAVIGATION RESTORE TEST';

async function openLiveConsole(page) {
  await signIn(page);
  await instrumentMicrophone(page);
  await stubWebSocket(page);
  await mockBackend(page);
  await page.goto('/console');

  await expect(page.getByTestId('target-mode-select')).toHaveValue('selected');
  await page.getByTestId('store-checkbox-UN').check();
  await page.getByTestId('store-checkbox-ASR').check();
  await page.getByTestId('campaign-name-input').fill(CAMPAIGN);

  await page.getByTestId('start-broadcast-btn').click();
  await expect(page.getByTestId('confirm-modal')).toBeVisible();
  await page.getByTestId('confirm-start-btn').click();
  await expect(page.getByTestId('stop-broadcast-btn')).toBeVisible();
}

test('the live campaign and Store selection survive navigating away and back',
     async ({ page }) => {
  await openLiveConsole(page);

  // Navigate to another EchoCast page, then back - real routing, which is
  // what unmounts the Console and discards its local state.
  await page.goto('/history');
  await expect(page).toHaveURL(/\/history/);
  await page.goto('/console');

  await expect(page.getByTestId('campaign-name-input')).toHaveValue(CAMPAIGN);
  await expect(page.getByTestId('store-checkbox-UN')).toBeChecked();
  await expect(page.getByTestId('store-checkbox-ASR')).toBeChecked();
  await expect(page.getByTestId('target-mode-select')).toHaveValue('selected');
  // And it is genuinely still the same live broadcast.
  await expect(page.getByTestId('stop-broadcast-btn')).toBeVisible();

  // A Store that was never a target must not appear selected.
  await expect(page.getByTestId('store-checkbox-DM')).not.toBeChecked();
});

test('the restored Console is read-only and starts no second broadcast',
     async ({ page }) => {
  const sessionPosts = [];
  page.on('request', (request) => {
    if (request.method() === 'POST'
        && request.url().endsWith('/api/broadcast/sessions')) {
      sessionPosts.push(request.url());
    }
  });

  await openLiveConsole(page);
  expect(sessionPosts).toHaveLength(1);

  await page.goto('/history');
  await page.goto('/console');

  await expect(page.getByTestId('campaign-name-input')).toHaveValue(CAMPAIGN);
  // Exactly one session across the whole flow: coming back re-rendered the
  // broadcast, it did not start another one.
  expect(sessionPosts).toHaveLength(1);

  // Existing behaviour, preserved: nothing about a live broadcast is editable,
  // so the operator cannot believe a checkbox changes what is on air.
  await expect(page.getByTestId('campaign-name-input')).toBeDisabled();
  await expect(page.getByTestId('target-mode-select')).toBeDisabled();
  await expect(page.getByTestId('store-checkbox-UN')).toBeDisabled();
});

test('the same live session drives the Store output controls after returning',
     async ({ page }) => {
  const audioControlUrls = [];
  page.on('request', (request) => {
    if (request.url().includes('/audio-control')) {
      audioControlUrls.push(request.url());
    }
  });

  await openLiveConsole(page);
  await page.goto('/history');
  await page.goto('/console');
  await expect(page.getByTestId('campaign-name-input')).toHaveValue(CAMPAIGN);

  // Whatever session id the controls bound to before navigating, they must
  // bind to the same one afterwards - not a new one invented on remount.
  expect(audioControlUrls.length).toBeGreaterThan(0);
  const sessionIds = new Set(
    audioControlUrls.map((url) => url.match(/sessions\/(\d+)\/audio-control/)[1]));
  expect([...sessionIds]).toHaveLength(1);
});

test('stopping clears the restored state, and a new draft starts empty',
     async ({ page }) => {
  await openLiveConsole(page);
  await page.goto('/history');
  await page.goto('/console');
  await expect(page.getByTestId('campaign-name-input')).toHaveValue(CAMPAIGN);

  await page.getByTestId('stop-broadcast-btn').click();
  await expect(page.getByTestId('start-broadcast-btn')).toBeVisible();

  // No ghost campaign and no ghost selection once the broadcast has ended.
  await expect(page.getByTestId('campaign-name-input')).toHaveValue('');
  await expect(page.getByTestId('campaign-name-input')).toBeEnabled();
  await expect(page.getByTestId('store-checkbox-UN')).not.toBeChecked();
  await expect(page.getByTestId('store-checkbox-ASR')).not.toBeChecked();

  // And leaving and returning does not resurrect it.
  await page.goto('/history');
  await page.goto('/console');
  await expect(page.getByTestId('campaign-name-input')).toHaveValue('');
  await expect(page.getByTestId('store-checkbox-UN')).not.toBeChecked();
  await expect(page.getByTestId('target-mode-select')).toHaveValue('selected');
});

test('an operator with no broadcast sees an ordinary empty draft',
     async ({ page }) => {
  await signIn(page);
  await mockBackend(page);
  await page.goto('/console');

  await expect(page.getByTestId('campaign-name-input')).toHaveValue('');
  await expect(page.getByTestId('campaign-name-input')).toBeEnabled();
  await expect(page.getByTestId('stat-selected')).toContainText('0');

  await page.goto('/history');
  await page.goto('/console');
  await expect(page.getByTestId('campaign-name-input')).toHaveValue('');
});
