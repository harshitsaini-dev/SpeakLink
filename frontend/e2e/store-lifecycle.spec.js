const { test, expect } = require('@playwright/test');
const { mockBackend, signIn, UN, ASR } = require('./support/backend');

// A Store is never deleted from the HQ console.
//
// It owns Receiver Devices, broadcast sessions, targets and event history;
// removing the row would destroy the only record of what was announced where
// and when. "Archive" retires it and keeps all of that readable, and the
// confirmation has to say so - an operator who believes Archive deletes things
// will avoid it and leave dead Stores in the broadcast list instead.
//
// Restore is deliberately a separate action from Re-enable, and returns a Store
// to DISABLED rather than ACTIVE. Un-retiring a Store is not a small decision
// and Re-enable is a small button; somebody has to look at its Devices first.

async function openStores(page, options = {}) {
  await signIn(page);
  const state = await mockBackend(page, options);
  await page.goto('/stores');
  await expect(page.getByTestId('store-mgmt-row-UN')).toBeVisible();
  return state;
}

test.describe('Lifecycle badges', () => {
  test('an active Store reads ACTIVE', async ({ page }) => {
    await openStores(page);
    await expect(page.getByTestId('store-mgmt-row-UN').getByTestId('lifecycle-ACTIVE')).toBeVisible();
  });

  test('a disabled Store reads DISABLED and offers Re-enable, not Disable', async ({ page }) => {
    await openStores(page, {
      stores: [{ ...UN, is_active: false, lifecycle_state: 'disabled' }, ASR],
    });
    const row = page.getByTestId('store-mgmt-row-UN');
    await expect(row.getByTestId('lifecycle-DISABLED')).toBeVisible();
    await expect(page.getByTestId('enable-store-UN')).toBeVisible();
    await expect(page.getByTestId('disable-store-UN')).toHaveCount(0);
  });

  test('an archived Store reads ARCHIVED and offers only Restore', async ({ page }) => {
    await openStores(page, {
      stores: [{ ...UN, is_active: false, lifecycle_state: 'archived' }, ASR],
    });
    const row = page.getByTestId('store-mgmt-row-UN');
    await expect(row.getByTestId('lifecycle-ARCHIVED')).toBeVisible();
    await expect(page.getByTestId('restore-store-UN')).toBeVisible();
    for (const absent of ['enable-store-UN', 'disable-store-UN', 'archive-store-UN',
                          'edit-store-UN', 'regen-token-UN']) {
      await expect(page.getByTestId(absent)).toHaveCount(0);
    }
  });
});

test.describe('Disable and re-enable', () => {
  test('disabling asks first and says nothing is deleted', async ({ page }) => {
    const state = await openStores(page);
    let message = '';
    page.on('dialog', (dialog) => { message = dialog.message(); dialog.accept(); });
    await page.getByTestId('disable-store-UN').click();

    await expect.poll(() => message).toContain('Nothing is deleted');
    await expect.poll(() => state.transitions).toEqual([{ id: 1, action: 'disable', to: 'disabled' }]);
    await expect(page.getByTestId('store-mgmt-row-UN').getByTestId('lifecycle-DISABLED')).toBeVisible();
  });

  test('dismissing the confirmation sends nothing', async ({ page }) => {
    const state = await openStores(page);
    page.on('dialog', (dialog) => dialog.dismiss());
    await page.getByTestId('disable-store-UN').click();
    await page.waitForTimeout(300);
    expect(state.transitions).toEqual([]);
  });

  test('re-enabling says no Device is promoted and no credential is created', async ({ page }) => {
    const state = await openStores(page, {
      stores: [{ ...UN, is_active: false, lifecycle_state: 'disabled' }, ASR],
    });
    let message = '';
    page.on('dialog', (dialog) => { message = dialog.message(); dialog.accept(); });
    await page.getByTestId('enable-store-UN').click();

    await expect.poll(() => message).toContain('No Receiver Device is promoted');
    await expect.poll(() => state.transitions).toEqual([{ id: 1, action: 'enable', to: 'active' }]);
    await expect(page.getByTestId('store-mgmt-row-UN').getByTestId('lifecycle-ACTIVE')).toBeVisible();
  });

  test('a Store in a live broadcast cannot be disabled, and says why', async ({ page }) => {
    await openStores(page, { liveStoreIds: [1] });
    page.on('dialog', (dialog) => dialog.accept());
    await page.getByTestId('disable-store-UN').click();

    await expect(page.getByTestId('stores-error')).toContainText('live broadcast');
    await expect(page.getByTestId('store-mgmt-row-UN').getByTestId('lifecycle-ACTIVE')).toBeVisible();
  });
});

test.describe('Archive and restore', () => {
  test('the archive confirmation promises history is kept', async ({ page }) => {
    await openStores(page);
    let message = '';
    page.on('dialog', (dialog) => { message = dialog.message(); dialog.dismiss(); });
    await page.getByTestId('archive-store-UN').click();

    await expect.poll(() => message).toContain('Nothing is deleted');
    await expect.poll(() => message).toContain('history all remain readable');
    await expect.poll(() => message).toContain('must be restored first');
  });

  test('archiving moves the Store to ARCHIVED', async ({ page }) => {
    const state = await openStores(page);
    page.on('dialog', (dialog) => dialog.accept());
    await page.getByTestId('archive-store-UN').click();

    await expect.poll(() => state.transitions).toEqual([{ id: 1, action: 'archive', to: 'archived' }]);
    await expect(page.getByTestId('store-mgmt-row-UN').getByTestId('lifecycle-ARCHIVED')).toBeVisible();
  });

  test('an archived Store cannot be re-enabled from here', async ({ page }) => {
    await openStores(page, {
      stores: [{ ...UN, is_active: false, lifecycle_state: 'archived' }, ASR],
    });
    await expect(page.getByTestId('enable-store-UN')).toHaveCount(0);
  });

  test('restore returns the Store to DISABLED, not ACTIVE', async ({ page }) => {
    const state = await openStores(page, {
      stores: [{ ...UN, is_active: false, lifecycle_state: 'archived' }, ASR],
    });
    let message = '';
    page.on('dialog', (dialog) => { message = dialog.message(); dialog.accept(); });
    await page.getByTestId('restore-store-UN').click();

    await expect.poll(() => message).toContain('returns to DISABLED, not active');
    await expect.poll(() => state.transitions).toEqual([{ id: 1, action: 'restore', to: 'disabled' }]);
    await expect(page.getByTestId('store-mgmt-row-UN').getByTestId('lifecycle-DISABLED')).toBeVisible();
    await expect(page.getByTestId('store-mgmt-row-UN').getByTestId('lifecycle-ACTIVE')).toHaveCount(0);
  });

  test('a restored Store then needs an explicit re-enable', async ({ page }) => {
    const state = await openStores(page, {
      stores: [{ ...UN, is_active: false, lifecycle_state: 'archived' }, ASR],
    });
    page.on('dialog', (dialog) => dialog.accept());
    await page.getByTestId('restore-store-UN').click();
    await expect(page.getByTestId('enable-store-UN')).toBeVisible();
    await page.getByTestId('enable-store-UN').click();
    await expect.poll(() => state.transitions.map((t) => t.action)).toEqual(['restore', 'enable']);
  });
});

test.describe('Editing a Store', () => {
  test('the edit form carries details only, never state or credentials', async ({ page }) => {
    await openStores(page);
    await page.getByTestId('edit-store-UN').click();
    const modal = page.getByTestId('edit-store-modal');
    await expect(modal).toBeVisible();
    await expect(modal).toContainText('Receiver credentials are never editable here');

    const html = await modal.innerHTML();
    expect(html).not.toContain('receiver_token');
    expect(html).not.toContain('is_active');
    await expect(page.getByTestId('edit-store-code-input')).toHaveValue('UN');
    await expect(page.getByTestId('edit-store-name-input')).toHaveValue('Uttam Nagar Old');
  });

  test('saving sends only the editable fields', async ({ page }) => {
    const state = await openStores(page);
    await page.getByTestId('edit-store-UN').click();
    await page.getByTestId('edit-store-name-input').fill('Uttam Nagar Old (renamed)');
    await page.getByTestId('edit-store-submit-btn').click();

    await expect.poll(() => state.edits.length).toBe(1);
    const sent = state.edits[0].payload;
    expect(Object.keys(sent).sort()).toEqual(
      ['city', 'is_online_store', 'region', 'store_code', 'store_name'],
    );
    expect(sent).not.toHaveProperty('is_active');
    expect(sent).not.toHaveProperty('receiver_token');
    await expect(page.getByTestId('store-mgmt-row-UN')).toContainText('renamed');
  });

  test('a duplicate Store code is reported, not swallowed', async ({ page }) => {
    await openStores(page);
    await page.getByTestId('edit-store-UN').click();
    await page.getByTestId('edit-store-code-input').fill('ASR');
    await page.getByTestId('edit-store-submit-btn').click();
    await expect(page.getByTestId('edit-store-error')).toContainText('already exists');
  });

  test('cancel closes without sending anything', async ({ page }) => {
    const state = await openStores(page);
    await page.getByTestId('edit-store-UN').click();
    await page.getByTestId('edit-store-cancel').click();
    await expect(page.getByTestId('edit-store-modal')).toHaveCount(0);
    expect(state.edits).toEqual([]);
  });
});

test.describe('Secrets and accessibility', () => {
  test('regenerating the legacy token never shows it', async ({ page }) => {
    const state = await openStores(page);
    let message = '';
    page.on('dialog', (dialog) => { message = dialog.message(); dialog.accept(); });
    await page.getByTestId('regen-token-UN').click();

    await expect.poll(() => message).toContain('never displayed');
    await expect.poll(() => state.regenerations).toEqual([1]);
    const body = (await page.textContent('body')) || '';
    expect(body).not.toContain('receiver_token');
  });

  test('no Store credential appears anywhere, in any lifecycle state', async ({ page }) => {
    await openStores(page, {
      stores: [
        { ...UN, lifecycle_state: 'archived', is_active: false },
        { ...ASR, lifecycle_state: 'disabled', is_active: false },
      ],
    });
    const html = await page.content();
    expect(html).not.toContain('receiver_token');
    expect(html).not.toContain('/receiver?token=');
    const hrefs = await page.$$eval('a', (nodes) => nodes.map((n) => n.getAttribute('href') || ''));
    for (const href of hrefs) expect(href).not.toContain('token=');
  });

  test('every lifecycle control is focusable and named', async ({ page }) => {
    await openStores(page);
    for (const testId of ['edit-store-UN', 'disable-store-UN', 'archive-store-UN', 'regen-token-UN']) {
      const control = page.getByTestId(testId);
      await expect(control).toBeVisible();
      await control.focus();
      await expect(control).toBeFocused();
      await expect(control).toHaveAttribute('aria-label', /.+/);
    }
  });

  test('a load failure is reported neutrally and recovers', async ({ page }) => {
    await signIn(page);
    // The full mock first: without /auth/me the app bounces to Login and the
    // page under test never renders, which is a different failure wearing this
    // test's name. The stores route is then overridden on top of it.
    await mockBackend(page);
    let failNext = true;
    // Store Management now loads through /stores/search - the server-side
    // filtered endpoint - so that is what has to fail for this test to be
    // about anything. The intent is unchanged: a load failure must be
    // reported, must not leak internals, and must recover.
    await page.route('**/api/stores/search*', async (route) => {
      if (failNext) {
        failNext = false;
        return route.fulfill({
          status: 503, contentType: 'application/json',
          body: JSON.stringify({ detail: 'unavailable' }),
        });
      }
      return route.fallback();
    });

    await page.goto('/stores');
    await expect(page.getByTestId('list-error')).toBeVisible();
    const message = (await page.getByTestId('list-error').textContent()) || '';
    expect(message.toLowerCase()).not.toContain('sql');
    expect(message.toLowerCase()).not.toContain('traceback');
    expect(message.toLowerCase()).not.toContain('503');

    await page.getByTestId('stores-refresh-btn').click();
    await expect(page.getByTestId('edit-store-UN')).toBeVisible();
    await expect(page.getByTestId('list-error')).toHaveCount(0);
  });
});
