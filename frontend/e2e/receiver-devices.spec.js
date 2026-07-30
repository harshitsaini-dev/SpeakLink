const { test, expect } = require('@playwright/test');
const { mockBackend, signIn, PRIMARY_DEVICE, STANDBY_DEVICE } = require('./support/backend');

// Receiver Device administration.
//
// Two secrets can appear on this page and each appears exactly once, ever: a
// one-time enrolment code and a rotated Device credential. The server keeps a
// verifier, not the value, so there is genuinely no way to show either again.
//
// That makes "shown once" a real property to test rather than a UI nicety: if
// the page put one in the URL, in localStorage, or re-rendered it after a
// reload, a credential that is supposed to exist in one place would be sitting
// in browser history on an HQ machine.

const DEVICES_URL = '/stores/1/devices';

async function openDevices(page) {
  await signIn(page);
  const state = await mockBackend(page);
  await page.goto(DEVICES_URL);
  await expect(page.getByTestId(`device-row-${PRIMARY_DEVICE.public_id}`)).toBeVisible();
  return state;
}

test.describe('Listing Devices', () => {
  test('both Devices are listed with their roles', async ({ page }) => {
    await openDevices(page);
    await expect(page.getByTestId(`device-row-${PRIMARY_DEVICE.public_id}`)).toContainText(
      'UN till 1 (primary)',
    );
    await expect(page.getByTestId(`device-row-${STANDBY_DEVICE.public_id}`)).toContainText(
      'UN till 2 (standby)',
    );
    await expect(page.getByTestId('device-role-PRIMARY')).toHaveCount(1);
    await expect(page.getByTestId('device-role-STANDBY')).toHaveCount(1);
  });

  test('the identifier is shortened rather than printed in full', async ({ page }) => {
    await openDevices(page);
    const row = page.getByTestId(`device-row-${PRIMARY_DEVICE.public_id}`);
    await expect(row).toContainText(PRIMARY_DEVICE.public_id.slice(0, 8));
    const body = (await page.textContent('body')) || '';
    expect(body).not.toContain(PRIMARY_DEVICE.public_id);
  });

  test('no credential material is anywhere on the page', async ({ page }) => {
    await openDevices(page);
    const html = await page.content();
    for (const forbidden of ['echocast_rcv_v', 'token_hash', 'hmac-sha256', 'receiver_token']) {
      expect(html).not.toContain(forbidden);
    }
  });

  test('an empty Store says so instead of showing an empty table', async ({ page }) => {
    await signIn(page);
    await mockBackend(page, { devices: [] });
    await page.goto(DEVICES_URL);
    await expect(page.getByTestId('devices-empty')).toBeVisible();
  });

  test('a failure is reported neutrally and recovers on retry', async ({ page }) => {
    await signIn(page);
    const state = await mockBackend(page, { deviceRolesStatus: 503 });
    await page.goto(DEVICES_URL);
    await expect(page.getByTestId('devices-error')).toBeVisible();

    const message = (await page.getByTestId('devices-error').textContent()) || '';
    expect(message.toLowerCase()).not.toContain('sql');
    expect(message.toLowerCase()).not.toContain('traceback');

    state.deviceRolesStatus = 200;
    await page.getByTestId('devices-refresh-btn').click();
    await expect(page.getByTestId(`device-row-${PRIMARY_DEVICE.public_id}`)).toBeVisible();
    await expect(page.getByTestId('devices-error')).toHaveCount(0);
  });
});

test.describe('The enrolment code is shown exactly once', () => {
  test('creating a code shows it with an expiry and a warning', async ({ page }) => {
    // The expiry used to be a static "15 minutes" written at the moment of
    // issue - already wrong a second later, and useless to somebody reading it
    // ten minutes in. It is a live countdown now, so this asserts the property
    // (an expiry is shown, and it is this code's) rather than the old wording.
    await openDevices(page);
    await page.getByTestId('create-enrolment-code-btn').click();

    const panel = page.getByTestId('enrolment-code-panel');
    await expect(panel).toBeVisible();
    await expect(page.getByTestId('issued-enrolment-code-value')).toContainText('ECHO-CODE-1');
    await expect(page.getByTestId('enrolment-code-countdown')).toContainText(/expires in \d+:\d{2}/);
    await expect(page.getByTestId('enrolment-code-state')).toHaveText('UNUSED');
    await expect(panel).toContainText('cannot be retrieved again');
  });

  test('the code never reaches the URL', async ({ page }) => {
    await openDevices(page);
    await page.getByTestId('create-enrolment-code-btn').click();
    await expect(page.getByTestId('issued-enrolment-code-value')).toContainText('ECHO-CODE-1');
    expect(page.url()).not.toContain('ECHO-CODE');
    expect(page.url()).toContain('/stores/1/devices');
  });

  test('the code is not retained after a reload', async ({ page }) => {
    await openDevices(page);
    await page.getByTestId('create-enrolment-code-btn').click();
    await expect(page.getByTestId('issued-enrolment-code-value')).toContainText('ECHO-CODE-1');

    await page.reload();
    await expect(page.getByTestId(`device-row-${PRIMARY_DEVICE.public_id}`)).toBeVisible();
    await expect(page.getByTestId('issued-enrolment-code')).toHaveCount(0);
    expect(await page.content()).not.toContain('ECHO-CODE-1');
  });

  test('the code is not retained after navigating away and back', async ({ page }) => {
    await openDevices(page);
    await page.getByTestId('create-enrolment-code-btn').click();
    await expect(page.getByTestId('issued-enrolment-code-value')).toContainText('ECHO-CODE-1');

    await page.getByTestId('back-to-stores').click();
    await expect(page.getByTestId('stores-page')).toBeVisible();
    await page.goto(DEVICES_URL);
    await expect(page.getByTestId(`device-row-${PRIMARY_DEVICE.public_id}`)).toBeVisible();
    await expect(page.getByTestId('issued-enrolment-code')).toHaveCount(0);
  });

  test('the code is never written to browser storage', async ({ page }) => {
    await openDevices(page);
    await page.getByTestId('create-enrolment-code-btn').click();
    await expect(page.getByTestId('issued-enrolment-code-value')).toContainText('ECHO-CODE-1');

    const stored = await page.evaluate(() =>
      JSON.stringify({
        local: Object.entries(window.localStorage),
        session: Object.entries(window.sessionStorage),
      }),
    );
    expect(stored).not.toContain('ECHO-CODE');
  });

  test('dismissing it removes it from the page', async ({ page }) => {
    await openDevices(page);
    await page.getByTestId('create-enrolment-code-btn').click();
    await page.getByTestId('issued-enrolment-code-dismiss').click();
    await expect(page.getByTestId('issued-enrolment-code')).toHaveCount(0);
  });
});

test.describe('Rotation', () => {
  test('rotating asks first, and a refusal sends nothing', async ({ page }) => {
    const state = await openDevices(page);
    page.on('dialog', (dialog) => dialog.dismiss());
    await page.getByTestId(`rotate-${PRIMARY_DEVICE.public_id}`).click();
    await page.waitForTimeout(300);
    expect(state.rotations).toHaveLength(0);
  });

  test('the confirmation says the old credential stops immediately', async ({ page }) => {
    await openDevices(page);
    let message = '';
    page.on('dialog', (dialog) => {
      message = dialog.message();
      dialog.dismiss();
    });
    await page.getByTestId(`rotate-${PRIMARY_DEVICE.public_id}`).click();
    await expect.poll(() => message).toContain('stops working immediately');
  });

  test('a confirmed rotation shows the new credential exactly once', async ({ page }) => {
    const state = await openDevices(page);
    page.on('dialog', (dialog) => dialog.accept());
    await page.getByTestId(`rotate-${PRIMARY_DEVICE.public_id}`).click();

    await expect(page.getByTestId('issued-credential-value')).toContainText('rotated-secret-shown-once');
    await expect(page.getByTestId('issued-credential')).toContainText('cannot be retrieved again');
    expect(state.rotations).toEqual([PRIMARY_DEVICE.public_id]);

    expect(page.url()).not.toContain('rotated-secret');
    await page.reload();
    await expect(page.getByTestId(`device-row-${PRIMARY_DEVICE.public_id}`)).toBeVisible();
    expect(await page.content()).not.toContain('rotated-secret-shown-once');
  });
});

test.describe('Promotion, disable and revoke', () => {
  test('promoting a standby makes it the only primary', async ({ page }) => {
    const state = await openDevices(page);
    page.on('dialog', (dialog) => dialog.accept());
    await page.getByTestId(`promote-${STANDBY_DEVICE.public_id}`).click();

    await expect
      .poll(() => state.promotions)
      .toEqual([STANDBY_DEVICE.public_id]);
    await expect(page.getByTestId('device-role-PRIMARY')).toHaveCount(1);
    await expect(page.getByTestId(`device-row-${STANDBY_DEVICE.public_id}`)).toContainText('Primary');
  });

  test('the primary has no Promote button, because it already is one', async ({ page }) => {
    await openDevices(page);
    await expect(page.getByTestId(`promote-${PRIMARY_DEVICE.public_id}`)).toHaveCount(0);
    await expect(page.getByTestId(`promote-${STANDBY_DEVICE.public_id}`)).toBeVisible();
  });

  test('disabling asks first and says the Store keeps working', async ({ page }) => {
    const state = await openDevices(page);
    let message = '';
    page.on('dialog', (dialog) => {
      message = dialog.message();
      dialog.accept();
    });
    await page.getByTestId(`disable-${STANDBY_DEVICE.public_id}`).click();
    await expect.poll(() => message).toContain('Store and every other Device keep working');
    await expect.poll(() => state.disables).toEqual([STANDBY_DEVICE.public_id]);
  });

  test('revoking asks first and says it cannot be undone', async ({ page }) => {
    const state = await openDevices(page);
    let message = '';
    page.on('dialog', (dialog) => {
      message = dialog.message();
      dialog.accept();
    });
    await page.getByTestId(`revoke-${STANDBY_DEVICE.public_id}`).click();
    await expect.poll(() => message).toContain('cannot be undone');
    await expect.poll(() => state.revokes).toEqual([STANDBY_DEVICE.public_id]);
  });

  test('losing the primary warns instead of silently promoting a standby', async ({ page }) => {
    await openDevices(page);
    page.on('dialog', (dialog) => dialog.accept());
    await page.getByTestId(`revoke-${PRIMARY_DEVICE.public_id}`).click();

    await expect(page.getByTestId('no-primary-warning')).toBeVisible();
    await expect(page.getByTestId('device-role-PRIMARY')).toHaveCount(0);
  });
});

test.describe('Reaching the page, and what it must not offer', () => {
  test('Store Management links to it with a Store id, not a credential', async ({ page }) => {
    await signIn(page);
    await mockBackend(page);
    await page.goto('/stores');
    const link = page.getByTestId('devices-UN');
    await expect(link).toBeVisible();
    expect(await link.getAttribute('href')).toBe('/stores/1/devices');
  });

  test('there is no Browser Receiver link and no shared Store token', async ({ page }) => {
    await openDevices(page);
    const body = ((await page.textContent('body')) || '').toLowerCase();
    expect(body).not.toContain('browser receiver');
    expect(body).not.toContain('receiver url');
    expect(body).not.toContain('shared token');
    const hrefs = await page.$$eval('a', (nodes) => nodes.map((n) => n.getAttribute('href') || ''));
    for (const href of hrefs) {
      expect(href).not.toContain('token=');
      expect(href).not.toContain('/receiver?');
    }
  });

  test('it never claims a speaker was heard', async ({ page }) => {
    await openDevices(page);
    const body = (await page.textContent('body')) || '';
    expect(body).not.toContain('SPEAKER_VERIFIED');
    expect(body.toLowerCase()).toContain('does not mean anyone heard it');
  });

  test('every control is reachable and labelled for a keyboard', async ({ page }) => {
    await openDevices(page);
    for (const testId of [
      'create-enrolment-code-btn',
      'devices-refresh-btn',
      `promote-${STANDBY_DEVICE.public_id}`,
      `rotate-${PRIMARY_DEVICE.public_id}`,
      `disable-${PRIMARY_DEVICE.public_id}`,
      `revoke-${PRIMARY_DEVICE.public_id}`,
    ]) {
      const control = page.getByTestId(testId);
      await expect(control).toBeVisible();
      await control.focus();
      await expect(control).toBeFocused();
    }
    // The icon-only row actions carry an accessible name rather than an icon alone.
    await expect(page.getByTestId(`revoke-${PRIMARY_DEVICE.public_id}`)).toHaveAttribute(
      'aria-label',
      /Revoke/,
    );
  });
});

test.describe('The enrolment record comes from the server, not the clock', () => {
  const UNUSED_RECORD = {
    id: 7,
    store_id: 1,
    state: 'UNUSED',
    created_at: '2026-07-30T09:00:00+00:00',
    expires_at: '2026-07-30T09:15:00+00:00',
    used_at: null,
    device_public_id: null,
    progress: ['CODE_CREATED'],
  };
  const USED_RECORD = {
    ...UNUSED_RECORD,
    state: 'USED',
    used_at: '2026-07-30T09:00:30+00:00',
    device_public_id: STANDBY_DEVICE.public_id,
    progress: ['CODE_CREATED', 'CODE_REDEEMED', 'DEVICE_CREATED', 'DEVICE_CONNECTED'],
  };

  async function openWithRecords(page, records, extra = {}) {
    await signIn(page);
    const state = await mockBackend(page, { enrolmentRecords: records, ...extra });
    await page.goto(DEVICES_URL);
    await expect(page.getByTestId(`device-row-${PRIMARY_DEVICE.public_id}`)).toBeVisible();
    return state;
  }

  test('an UNUSED record keeps the countdown and shows only CODE_CREATED', async ({ page }) => {
    await openWithRecords(page, [UNUSED_RECORD]);
    await page.getByTestId('create-enrolment-code-btn').click();

    await expect(page.getByTestId('enrolment-code-state')).toHaveText('UNUSED');
    await expect(page.getByTestId('enrolment-code-countdown')).toBeVisible();
    await expect(page.getByTestId('setup-stage-CODE_CREATED')).toHaveAttribute('data-reached', 'true');
    await expect(page.getByTestId('setup-stage-CODE_REDEEMED')).toHaveAttribute('data-reached', 'false');
  });

  test('a USED record shows USED and removes the code from the page', async ({ page }) => {
    await openWithRecords(page, [USED_RECORD]);
    await page.getByTestId('create-enrolment-code-btn').click();

    await expect(page.getByTestId('enrolment-code-state')).toHaveText('USED');
    await expect(page.getByTestId('enrolment-code-used')).toBeVisible();
    // The value is gone the moment it is spent.
    await expect(page.getByTestId('issued-enrolment-code-value')).toHaveCount(0);
    await expect(page.locator('body')).not.toContainText('ECHO-CODE-1');
  });

  test('a USED record links the Device it enrolled', async ({ page }) => {
    await openWithRecords(page, [USED_RECORD]);
    await page.getByTestId('create-enrolment-code-btn').click();
    await expect(page.getByTestId('enrolled-device-public-id')).toHaveText(
      STANDBY_DEVICE.public_id,
    );
  });

  test('a USED record shows only the stages the server proved', async ({ page }) => {
    await openWithRecords(page, [USED_RECORD]);
    await page.getByTestId('create-enrolment-code-btn').click();

    for (const stage of ['CODE_CREATED', 'CODE_REDEEMED', 'DEVICE_CREATED', 'DEVICE_CONNECTED']) {
      await expect(page.getByTestId(`setup-stage-${stage}`)).toHaveAttribute('data-reached', 'true');
    }
    // Not proved, so not claimed - even though every earlier stage was reached.
    await expect(page.getByTestId('setup-stage-PRIMARY_ASSIGNED')).toHaveAttribute(
      'data-reached',
      'false',
    );
  });

  test('a code the server calls USED is never relabelled EXPIRED by the clock', async ({ page }) => {
    // The exact misreport this endpoint exists to prevent: a code redeemed
    // seconds into its life, viewed after the countdown has run out. It IS
    // enrolled; showing EXPIRED would say the setup failed.
    await openWithRecords(page, [USED_RECORD], { enrolmentCodeSeconds: 1 });
    await page.getByTestId('create-enrolment-code-btn').click();
    await expect(page.getByTestId('enrolment-code-state')).toHaveText('USED');
    await page.waitForTimeout(1500);
    await expect(page.getByTestId('enrolment-code-state')).toHaveText('USED');
    await expect(page.getByTestId('enrolment-code-expired')).toHaveCount(0);
  });

  test('a failing status poll leaves the last known state rather than reverting', async ({ page }) => {
    await openWithRecords(page, [USED_RECORD]);
    await page.getByTestId('create-enrolment-code-btn').click();
    await expect(page.getByTestId('enrolment-code-state')).toHaveText('USED');

    // Break the endpoint after the page has already learned the truth.
    await page.route('**/api/stores/*/enrollment-codes', (route) =>
      route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"nope"}' }),
    );
    await page.waitForTimeout(3500);
    await expect(page.getByTestId('enrolment-code-state')).toHaveText('USED');
  });

  test('the status response never carries a code or a hash', async ({ page }) => {
    const bodies = [];
    await signIn(page);
    await mockBackend(page, { enrolmentRecords: [USED_RECORD] });
    page.on('response', async (response) => {
      if (response.url().includes('/enrollment-codes') && response.request().method() === 'GET') {
        bodies.push(await response.text().catch(() => ''));
      }
    });
    await page.goto(DEVICES_URL);
    await expect(page.getByTestId(`device-row-${PRIMARY_DEVICE.public_id}`)).toBeVisible();
    await page.getByTestId('create-enrolment-code-btn').click();
    await expect(page.getByTestId('enrolment-code-state')).toHaveText('USED');

    expect(bodies.length).toBeGreaterThan(0);
    for (const body of bodies) {
      expect(body).not.toContain('ECHO-CODE');
      expect(body.toLowerCase()).not.toContain('code_hash');
      expect(body).not.toContain('echocast_rcv_v');
    }
  });

  test('no secret reaches the URL or browser storage', async ({ page }) => {
    await openWithRecords(page, [USED_RECORD]);
    await page.getByTestId('create-enrolment-code-btn').click();
    await expect(page.getByTestId('enrolment-code-state')).toHaveText('USED');

    expect(page.url()).not.toContain('ECHO-CODE');
    const stored = await page.evaluate(() => {
      const all = [];
      for (let i = 0; i < localStorage.length; i += 1) {
        all.push(localStorage.getItem(localStorage.key(i)) || '');
      }
      for (let i = 0; i < sessionStorage.length; i += 1) {
        all.push(sessionStorage.getItem(sessionStorage.key(i)) || '');
      }
      return all.join('|');
    });
    expect(stored).not.toContain('ECHO-CODE');
    expect(stored).not.toContain('echocast_rcv_v');
  });

  test('the page still works when the status endpoint is unavailable', async ({ page }) => {
    // A page that cannot read the evidence must fall back to the clock, not
    // break. Nothing about the Device list depends on this poll.
    await openWithRecords(page, [], { enrolmentCodeStatus: 503 });
    await page.getByTestId('create-enrolment-code-btn').click();
    await expect(page.getByTestId('enrolment-code-state')).toHaveText('UNUSED');
    await expect(page.getByTestId('issued-enrolment-code-value')).toContainText('ECHO-CODE-1');
    await expect(page.getByTestId('setup-progress')).toHaveCount(0);
  });
});
