const { test, expect } = require('@playwright/test');
const { mockBackend, signIn, STORES } = require('./support/backend');

// Store Management used to render, for every Store, a "Copy" button that put
// ${origin}/receiver?token=${receiver_token} on the clipboard. That is a
// long-lived credential shared by every Receiver in that Store, travelling
// through a URL - so through clipboards, chat messages, browser history and any
// log that saw the link. Revoking it kicked every Receiver at once.
//
// A Receiver computer will earn its own credential through one-time enrolment
// instead. Until then this page must simply never show one.

test.describe('Store Management never reveals a Receiver credential', () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page);
    await mockBackend(page);
    await page.goto('/stores');
    await expect(page.getByTestId('store-mgmt-row-UN')).toBeVisible();
  });

  test('no Store credential appears anywhere on the page', async ({ page }) => {
    const body = (await page.textContent('body')) || '';
    const html = await page.content();
    for (const store of STORES) {
      // The mocked catalog carries no receiver_token at all; assert on the real
      // field name too, so a future page that starts rendering it fails here.
      expect(body).not.toContain('receiver_token');
      expect(html).not.toContain('/receiver?token=');
      expect(body).not.toContain(store.store_code + '-token');
    }
  });

  test('there is no copy-URL control', async ({ page }) => {
    await expect(page.getByTestId('copy-url-UN')).toHaveCount(0);
    const body = ((await page.textContent('body')) || '').toLowerCase();
    expect(body).not.toContain('receiver url');
    expect(body).not.toContain('kiosk url');
  });

  test('rotating a credential is still possible without showing it', async ({ page }) => {
    // Regeneration stays: an operator must be able to revoke. What changes is
    // that the new value is never rendered.
    await expect(page.getByTestId('regen-token-UN')).toBeVisible();
  });

  test('no link on the page carries a token query parameter', async ({ page }) => {
    const hrefs = await page.$$eval('a', (nodes) => nodes.map((node) => node.getAttribute('href') || ''));
    for (const href of hrefs) {
      expect(href).not.toContain('token=');
    }
  });
});

test.describe('The browser Receiver page is not reachable', () => {
  test('/receiver no longer routes to a credential-consuming page', async ({ page }) => {
    await signIn(page);
    await mockBackend(page);
    await page.goto('/receiver?token=not-a-real-credential');

    // The catch-all sends unknown paths to the console. What must not happen is
    // a page that reads that token and opens a socket with it.
    await expect(page).not.toHaveURL(/\/receiver\b/);
    const html = await page.content();
    expect(html).not.toContain('not-a-real-credential');
  });
});
