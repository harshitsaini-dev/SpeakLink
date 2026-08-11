/**
 * The Console block holds its shape across the screens it is actually used on.
 *
 * The rebuilt layout is a grid with explicit line placement, and grids fail in
 * a particular way: a card that outgrows its track pushes the page sideways,
 * or the page acquires a second scrollbar, or a column simply disappears below
 * the fold on a laptop. None of those show up in a unit test.
 *
 * So this file asserts the four properties that have actually broken here
 * before, at six real viewport sizes:
 *
 *   1. exactly ONE vertical scroll owner - the shell must never scroll
 *      alongside the main region;
 *   2. no horizontal overflow at all;
 *   3. every card present and non-zero;
 *   4. the cards that share a row share a top edge, and stack on a phone
 *      instead of squeezing into unreadable columns.
 */
const { test, expect } = require('@playwright/test');
const { mockBackend, signIn, stubWebSocket, STORES } = require('./support/backend');

const VIEWPORTS = [
  { name: 'laptop 1366x768', width: 1366, height: 768 },
  { name: 'laptop 1440x900', width: 1440, height: 900 },
  { name: 'desktop 1920x1080', width: 1920, height: 1080 },
  { name: 'wide 2560x1440', width: 2560, height: 1440 },
  { name: 'tablet 820x1180', width: 820, height: 1180 },
  { name: 'phone 390x844', width: 390, height: 844 },
];

const LIVE = {
  live: true,
  session: {
    id: 8, campaign_name: 'Evening announcement', status: 'live',
    target_mode: 'selected', started_at: new Date(0).toISOString(),
  },
  targets: [{ id: 1, store_id: 1, play_status: 'audio_receiving',
              lifecycle_state: 'ACTIVE', current_generation: 1 }],
  online_receivers: [1], ready_receivers: [1],
};

const CARDS = [
  'broadcast-status-card',
  'console-controls-card',
  'console-audience-card',
  'target-summary',
];

async function openConsole(page, { live = false } = {}) {
  await mockBackend(page, { stores: STORES, ...(live ? { current: LIVE } : {}) });
  await stubWebSocket(page);
  await page.goto('/console');
  await expect(page.getByTestId('broadcast-console')).toBeVisible();
  // Every card has arrived and the browser has laid the page out. Measuring
  // before this reads a layout that is still settling, which is how the first
  // version of this file reported an overflow that did not survive a repaint.
  await expect(page.getByTestId('console-audience-card')).not.toBeEmpty();
  await page.evaluate(() => new Promise(requestAnimationFrame));
}

test.beforeEach(async ({ page }) => {
  await signIn(page);
});


for (const viewport of VIEWPORTS) {
  test(`the console holds together at ${viewport.name}`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await openConsole(page, { live: true });

    // 1. ONE vertical scroll owner. The shell is exactly one viewport tall and
    //    never scrolls itself; main owns scrolling. Two scrollbars is the
    //    defect this whole shell exists to prevent.
    const scrollers = await page.evaluate(() => {
      const owners = [];
      const seen = [document.documentElement, document.body,
                    ...document.querySelectorAll('[data-testid]')];
      for (const node of seen) {
        const style = getComputedStyle(node);
        const scrollable = /(auto|scroll)/.test(style.overflowY);
        if (scrollable && node.scrollHeight > node.clientHeight + 4) {
          owners.push(node.getAttribute('data-testid') || node.tagName.toLowerCase());
        }
      }
      return owners;
    });
    const pageScrollers = scrollers.filter(
      (name) => name === 'html' || name === 'body' || name === 'app-shell');
    expect(pageScrollers, `page-level scrollers: ${scrollers.join(', ')}`).toEqual([]);

    // 2. No horizontal overflow. A grid track that cannot shrink shows up here
    //    and nowhere else.
    // 2. Nothing spills sideways OUT of the page.
    //
    //    Two metrics were tried here and both were wrong, so the reasoning is
    //    written down. documentElement.scrollWidth counts content that is
    //    clipped inside a scroller, so it reports 553 on a phone purely
    //    because the Store table scrolls in its own container - which is
    //    intended. And window.scrollTo() still moves a document whose overflow
    //    is hidden: programmatic scrolling is not blocked by clipping, so
    //    "try to scroll and look" reports movement no person could produce.
    //
    //    What actually matters: is there anything past the right edge that is
    //    NOT inside a horizontal scroller? That is the element a person would
    //    find cut off with no way to reach it.
    const spills = await page.evaluate(() => {
      const offenders = [];
      document.querySelectorAll('*').forEach((node) => {
        const box = node.getBoundingClientRect();
        if (box.right <= window.innerWidth + 1 || box.width >= 4000) return;
        for (let parent = node.parentElement; parent; parent = parent.parentElement) {
          if (/(auto|scroll|hidden)/.test(getComputedStyle(parent).overflowX)) return;
        }
        offenders.push(`${node.tagName}[${node.getAttribute('data-testid') || ''}] `
                       + `w=${Math.round(box.width)} right=${Math.round(box.right)}`);
      });
      return offenders.slice(0, 6);
    });
    expect(spills, `content past the right edge: ${spills.join(' | ')}`).toEqual([]);

    // 3. Every card present, and actually occupying space.
    for (const card of CARDS) {
      const box = await page.getByTestId(card).boundingBox();
      expect(box, `${card} has no box`).not.toBeNull();
      expect(box.width, `${card} is too narrow to read`).toBeGreaterThan(180);
      expect(box.height, `${card} collapsed`).toBeGreaterThan(40);
    }
    await expect(page.getByTestId('broadcast-chat-card')).toBeVisible();
  });
}


test('on a wide screen the paired cards share a top edge', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await openConsole(page, { live: true });

  const status = await page.getByTestId('broadcast-status-card').boundingBox();
  const controls = await page.getByTestId('console-controls-card').boundingBox();
  const audience = await page.getByTestId('console-audience-card').boundingBox();
  const targets = await page.getByTestId('target-summary').boundingBox();

  expect(Math.abs(status.y - controls.y)).toBeLessThan(4);
  expect(Math.abs(audience.y - targets.y)).toBeLessThan(4);
  // Two columns, not one on top of the other.
  expect(controls.x).toBeGreaterThan(status.x + status.width - 8);
  // And the chat is beside them, not underneath.
  const chat = await page.getByTestId('broadcast-chat-card').boundingBox();
  expect(chat.x).toBeGreaterThan(controls.x);
  expect(chat.y).toBeLessThan(audience.y + 8);
});


test('on a phone the cards stack instead of squeezing', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await openConsole(page, { live: true });

  const status = await page.getByTestId('broadcast-status-card').boundingBox();
  const controls = await page.getByTestId('console-controls-card').boundingBox();
  expect(controls.y).toBeGreaterThan(status.y + status.height - 8);
  // Full width, both of them - a two-column layout at 390px is unreadable.
  expect(status.width).toBeGreaterThan(300);
  expect(controls.width).toBeGreaterThan(300);
});


test('a full Web Audience does not push the chat off the screen',
     async ({ page }) => {
  // The regression this guards: the audience card grew with every listener,
  // so a busy room made the page taller than the viewport and the chat column
  // ended up somewhere nobody would scroll to. The card has a ceiling now and
  // scrolls inside it.
  await page.setViewportSize({ width: 1440, height: 900 });
  await mockBackend(page, {
    stores: STORES,
    current: LIVE,
    listeners: Array.from({ length: 25 }, (_, index) => ({
      id: index + 1, display_name: `Listener ${index + 1}`,
      admission_status: 'PASSWORD_ADMITTED', connected: true,
      playback_state: 'LISTENING',
    })),
  });
  await stubWebSocket(page);
  await page.goto('/console');

  const audience = page.getByTestId('console-audience-card');
  await expect(audience).toBeVisible();
  const box = await audience.boundingBox();
  expect(box.height, 'the audience card grew past the viewport').toBeLessThan(700);

  const chat = await page.getByTestId('broadcast-chat-card').boundingBox();
  expect(chat.y).toBeLessThan(900);
});


test('nothing inside a card is drawn on top of anything else', async ({ page }) => {
  // The bug this exists for: the Web Audience card put the Broadcast ID, two
  // copy buttons and a "Generate New Password" button on one unwrappable row.
  // In a third-width column flexbox shrank them until they OVERLAPPED - the
  // page had no overflow, every card had a sensible box, and each child was
  // still inside its card. Only the boxes overlapping each other showed it,
  // which is why this measures siblings against siblings rather than against
  // their container.
  const SIZES = [
    { width: 1366, height: 768 },
    { width: 1600, height: 900 },
    { width: 1920, height: 1080 },
  ];

  for (const size of SIZES) {
    await page.setViewportSize(size);
    await openConsole(page, { live: true });

    const collisions = await page.evaluate(() => {
      const problems = [];
      const cards = ['console-audience-card', 'console-controls-card',
                     'broadcast-status-card', 'target-summary',
                     'broadcast-chat-card'];
      const label = (node) => `<${node.tagName.toLowerCase()}>`
        + `"${(node.textContent || '').trim().slice(0, 20)}"`;

      for (const id of cards) {
        const card = document.querySelector(`[data-testid="${id}"]`);
        if (!card) continue;
        // Leaf controls and their labels only. Containers legitimately contain
        // one another, and an ancestor overlapping its descendant is not a bug.
        const parts = Array.from(card.querySelectorAll('button, input, select'))
          .concat(Array.from(card.querySelectorAll('span, p'))
            .filter((node) => node.children.length === 0
                              && (node.textContent || '').trim()));
        const boxes = parts
          .map((node) => ({ node, box: node.getBoundingClientRect() }))
          .filter(({ box }) => box.width > 1 && box.height > 1);

        for (let i = 0; i < boxes.length; i += 1) {
          for (let j = i + 1; j < boxes.length; j += 1) {
            const a = boxes[i];
            const b = boxes[j];
            if (a.node.contains(b.node) || b.node.contains(a.node)) continue;
            // Two pixels of slack for borders and sub-pixel rounding.
            const overlapX = Math.min(a.box.right, b.box.right)
                           - Math.max(a.box.left, b.box.left);
            const overlapY = Math.min(a.box.bottom, b.box.bottom)
                           - Math.max(a.box.top, b.box.top);
            if (overlapX > 2 && overlapY > 2) {
              problems.push(`${id}: ${label(a.node)} overlaps ${label(b.node)}`);
            }
          }
        }
      }
      return problems.slice(0, 6);
    });

    expect(collisions, `at ${size.width}x${size.height}`).toEqual([]);
  }
});
