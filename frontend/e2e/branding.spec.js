const { test, expect } = require('@playwright/test');
const { mockBackend, signIn } = require('./support/backend');

// The product is EchoCast Live. It shipped as "Emergent | Fullstack App",
// which is the template it started from.
//
// The second half of this file is not a branding question. index.html loaded
// PostHog with session recording enabled and `recordCrossOriginIframes: true`,
// under a hard-coded project key. Session recording captures the operator's
// screen - and this console displays, exactly once and never again, one-time
// Receiver enrolment codes and rotated Device credentials. The server keeps a
// verifier rather than the value precisely so those cannot be recovered later;
// streaming the screen that shows them to a third party would have undone that
// at the last possible moment, silently.

test.describe('EchoCast Live branding', () => {
  test('the browser title is exactly EchoCast Live', async ({ page }) => {
    await signIn(page);
    await mockBackend(page);
    await page.goto('/console');
    await expect(page).toHaveTitle('EchoCast Live');
  });

  test('the title is right on the login page too, before anyone signs in', async ({ page }) => {
    await page.goto('/login');
    await expect(page).toHaveTitle('EchoCast Live');
  });

  test('no Emergent branding is visible anywhere in the shipped page', async ({ page }) => {
    await signIn(page);
    await mockBackend(page);
    await page.goto('/console');

    const html = await page.content();
    expect(html).not.toContain('Emergent | Fullstack');
    expect(html).not.toContain('emergent.sh');
    const body = (await page.textContent('body')) || '';
    expect(body).not.toContain('Emergent');
  });

  test('the description names the product', async ({ page }) => {
    await page.goto('/login');
    const description = await page.getAttribute('meta[name="description"]', 'content');
    expect(description).toContain('EchoCast Live');
    expect(description).not.toContain('emergent');
  });
});

test.describe('No third-party recorder watches the HQ console', () => {
  test('no script is loaded from an external analytics host', async ({ page }) => {
    const external = [];
    page.on('request', (request) => {
      const url = request.url();
      if (/emergent\.sh|posthog\.com|i\.posthog/.test(url)) external.push(url);
    });

    await signIn(page);
    await mockBackend(page);
    await page.goto('/console');
    await page.waitForTimeout(500);

    expect(external).toEqual([]);
  });

  test('the page defines no session recorder', async ({ page }) => {
    await signIn(page);
    await mockBackend(page);
    await page.goto('/console');

    const present = await page.evaluate(() => ({
      posthog: typeof window.posthog !== 'undefined',
      rrweb: typeof window.rrweb !== 'undefined',
    }));
    expect(present.posthog).toBe(false);
    expect(present.rrweb).toBe(false);
  });

  test('the page source carries no analytics project key', async ({ page }) => {
    await page.goto('/login');
    const html = await page.content();
    // The key that was hard-coded here. Its shape, not the value.
    expect(html).not.toMatch(/phc_[A-Za-z0-9]{20,}/);
    expect(html).not.toContain('session_recording');
    expect(html).not.toContain('recordCrossOriginIframes');
  });

  test('the Device page, which shows credentials once, loads no recorder', async ({ page }) => {
    // The page this matters most on: a rotated credential is rendered here and
    // exists nowhere else afterwards.
    const external = [];
    page.on('request', (request) => {
      if (/posthog|emergent\.sh/.test(request.url())) external.push(request.url());
    });

    await signIn(page);
    await mockBackend(page);
    await page.goto('/stores/1/devices');
    await page.waitForTimeout(500);

    expect(external).toEqual([]);
  });
});
