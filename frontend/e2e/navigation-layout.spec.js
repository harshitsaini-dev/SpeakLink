/**
 * The HQ navigation must not scroll away with the page.
 *
 * On the real HQ the operator scrolled a long page and the upper menu items left
 * the viewport. The shell was `min-h-screen flex`, which GROWS with its content,
 * so the whole document scrolled at body level - and the sidebar, being
 * `md:static`, travelled with it. `main` had no vertical scroll of its own, so it
 * was the body that had to move.
 *
 * The fix is structural: the shell owns exactly the viewport, the sidebar is
 * sticky and full height, and `main` scrolls independently. These tests assert
 * the consequence a person actually notices - the sidebar's top stays where it
 * is while the content moves.
 */

const { test, expect } = require('@playwright/test');
const { mockBackend, signIn } = require('./support/backend');

/** Make the main column definitely taller than the viewport. */
async function growMainContent(page) {
  await page.evaluate(() => {
    const main = document.querySelector('main');
    const filler = document.createElement('div');
    filler.setAttribute('data-testid', 'scroll-filler');
    filler.style.height = '4000px';
    main.appendChild(filler);
  });
}

async function sidebarTop(page) {
  return page.evaluate(() => document.querySelector('aside').getBoundingClientRect().top);
}

test.describe('HQ navigation stays visible', () => {
  test.beforeEach(async ({ page }) => {
    // signIn only seeds the token; the Layout only exists on a signed-in route,
    // so the test has to actually land on one before <main> is in the document.
    await mockBackend(page);
    await signIn(page);
    await page.goto('/console');
    await page.waitForSelector('main', { timeout: 15000 });
    await page.waitForSelector('aside nav a', { timeout: 15000 });
  });

  test('scrolling the main content does not move the sidebar', async ({ page }) => {
    await growMainContent(page);
    const before = await sidebarTop(page);

    await page.locator('main').evaluate((el) => el.scrollTo(0, el.scrollHeight));
    await page.waitForTimeout(150);

    expect(await sidebarTop(page)).toBeCloseTo(before, 0);
  });

  test('the branding stays visible after scrolling to the bottom', async ({ page }) => {
    await growMainContent(page);
    await page.locator('main').evaluate((el) => el.scrollTo(0, el.scrollHeight));
    await page.waitForTimeout(150);

    await expect(page.getByText('EchoCast', { exact: true }).first()).toBeInViewport();
  });

  test('every navigation item stays reachable after scrolling', async ({ page }) => {
    await growMainContent(page);
    await page.locator('main').evaluate((el) => el.scrollTo(0, el.scrollHeight));
    await page.waitForTimeout(150);

    const links = page.locator('aside nav a');
    const count = await links.count();
    expect(count).toBeGreaterThan(0);
    for (let index = 0; index < count; index += 1) {
      await expect(links.nth(index)).toBeVisible();
    }
  });

  test('the signed-in user and Log out stay reachable', async ({ page }) => {
    await growMainContent(page);
    await page.locator('main').evaluate((el) => el.scrollTo(0, el.scrollHeight));
    await page.waitForTimeout(150);

    await expect(page.getByText('Signed in as')).toBeInViewport();
    await expect(page.getByRole('button', { name: /log out/i })).toBeInViewport();
  });

  test('the body itself never scrolls', async ({ page }) => {
    await growMainContent(page);

    const overflow = await page.evaluate(() => ({
      body: document.body.scrollHeight - window.innerHeight,
      main: document.querySelector('main').scrollHeight > document.querySelector('main').clientHeight,
    }));

    expect(overflow.main).toBe(true);
    expect(overflow.body).toBeLessThanOrEqual(1);
  });

  test('there is no horizontal overflow at 1366x768', async ({ page }) => {
    await page.setViewportSize({ width: 1366, height: 768 });
    await growMainContent(page);

    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
  });

  for (const size of [{ width: 1366, height: 768 }, { width: 1920, height: 1080 }]) {
    test(`the sidebar holds at ${size.width}x${size.height}`, async ({ page }) => {
      await page.setViewportSize(size);
      await growMainContent(page);
      const before = await sidebarTop(page);

      await page.locator('main').evaluate((el) => el.scrollTo(0, el.scrollHeight));
      await page.waitForTimeout(150);

      expect(await sidebarTop(page)).toBeCloseTo(before, 0);
    });
  }

  test('the active route stays highlighted after scrolling', async ({ page }) => {
    await growMainContent(page);
    await page.locator('main').evaluate((el) => el.scrollTo(0, el.scrollHeight));
    await page.waitForTimeout(150);

    const active = page.locator('aside nav a[aria-current="page"], aside nav a.bg-slate-800');
    await expect(active.first()).toBeVisible();
  });

  test('the sidebar scrolls internally rather than pushing items off', async ({ page }) => {
    // A short viewport is the case where the nav list genuinely cannot fit.
    await page.setViewportSize({ width: 1366, height: 420 });

    const scrolls = await page.evaluate(() => {
      const nav = document.querySelector('aside nav');
      return { canScroll: nav.scrollHeight > nav.clientHeight,
               overflow: getComputedStyle(nav).overflowY };
    });
    expect(['auto', 'scroll']).toContain(scrolls.overflow);
  });
});
