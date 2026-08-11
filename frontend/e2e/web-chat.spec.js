/**
 * The host's chat, in a real browser.
 *
 * The jest tests already prove the wiring. What a browser adds is the thing an
 * operator actually experiences: that the card sits in the console beside the
 * broadcast without growing it, that a long conversation scrolls INSIDE the
 * card rather than pushing the page around, and that turning chat off changes
 * what the audience may do without silencing the person running it.
 *
 * The backend is mocked. Whether a private message is really unreadable by
 * another listener is proved against the database in
 * backend/tests/test_web_chat.py - that is a server property, and a browser
 * cannot prove it.
 */
const { test, expect } = require('@playwright/test');
const { mockBackend, signIn, stubWebSocket, STORES } = require('./support/backend');

const SESSION_ID = 8;

function liveWith(messages, settings = {}) {
  return {
    current: {
      live: true,
      session: {
        id: SESSION_ID, campaign_name: 'Evening announcement', status: 'live',
        target_mode: 'only_with_link', started_at: new Date(0).toISOString(),
      },
      targets: [], online_receivers: [], ready_receivers: [],
    },
    chat: {
      chat_enabled: true, chat_mode: 'PUBLIC',
      messages, ...settings,
    },
  };
}

function listenerMessage(id, body, overrides = {}) {
  return {
    id, participant_id: 5, author_kind: 'LISTENER', author_name: 'Harshit',
    body, deleted: false, visibility: 'PUBLIC',
    created_at: '2026-08-11T13:53:00Z', has_image: false, ...overrides,
  };
}

test.beforeEach(async ({ page }) => {
  await signIn(page);
});


test('FLOW A: the host reads the audience and answers', async ({ page }) => {
  await mockBackend(page, {
    stores: STORES,
    ...liveWith([listenerMessage(1, 'We cannot hear you at the till')]),
  });
  await stubWebSocket(page);
  await page.goto('/console');

  await expect(page.getByTestId('broadcast-chat-card')).toBeVisible();
  await expect(page.getByTestId('chat-message-1')).toContainText('Harshit');
  await expect(page.getByTestId('chat-message-1')).toContainText('cannot hear you');

  await page.getByTestId('chat-input').fill('Repeating it now');
  await page.getByTestId('chat-send').click();

  await expect(page.getByTestId('chat-messages')).toContainText('Repeating it now');
  // The composer clears, so the same reply cannot be sent twice by reflex.
  await expect(page.getByTestId('chat-input')).toHaveValue('');
});


test('FLOW B: turning chat off stops the audience, not the host',
     async ({ page }) => {
  await mockBackend(page, { stores: STORES, ...liveWith([]) });
  await stubWebSocket(page);
  await page.goto('/console');

  await page.getByTestId('chat-toggle-enabled').click();

  await expect(page.getByTestId('chat-off-badge')).toBeVisible();
  await expect(page.getByTestId('chat-toggle-enabled')).toContainText('Turn chat on');
  // The operator may still need to answer the last question.
  await expect(page.getByTestId('chat-input')).toBeEnabled();
  await expect(page.getByTestId('chat-send')).toBeDisabled();   // nothing typed yet
  await page.getByTestId('chat-input').fill('Chat is closing, thank you');
  await expect(page.getByTestId('chat-send')).toBeEnabled();
});


test('FLOW C: private mode is announced, and does not rewrite what was said',
     async ({ page }) => {
  await mockBackend(page, {
    stores: STORES,
    ...liveWith([listenerMessage(1, 'said in public')]),
  });
  await stubWebSocket(page);
  await page.goto('/console');

  await page.getByTestId('chat-toggle-mode').click();

  await expect(page.getByTestId('chat-private-badge')).toBeVisible();
  await expect(page.getByTestId('chat-toggle-mode')).toContainText('Make public');
  // The message sent while the room was public is still public. Retro-hiding
  // it would fool nobody who was in the room.
  await expect(page.getByTestId('chat-private-1')).toHaveCount(0);
});


test('FLOW D: a removed message keeps its place and loses its words',
     async ({ page }) => {
  await mockBackend(page, {
    stores: STORES,
    ...liveWith([listenerMessage(1, 'something regrettable')]),
  });
  await stubWebSocket(page);
  await page.goto('/console');

  await page.getByTestId('chat-message-1').hover();
  await page.getByTestId('chat-delete-1').click();

  await expect(page.getByTestId('chat-removed-1')).toBeVisible();
  await expect(page.getByTestId('chat-message-1')).toContainText('Harshit');
  await expect(page.getByTestId('chat-message-1')).not.toContainText('regrettable');
});


test('FLOW E: a long conversation scrolls inside the card, not down the page',
     async ({ page }) => {
  // The property that keeps the console usable: the card must not grow with
  // the conversation, or every message would push Targets and Controls
  // further down the page.
  const many = Array.from({ length: 40 },
    (_, index) => listenerMessage(index + 1, `message number ${index + 1}`));
  await mockBackend(page, { stores: STORES, ...liveWith(many) });
  await stubWebSocket(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/console');

  const card = page.getByTestId('broadcast-chat-card');
  await expect(card).toBeVisible();
  const cardHeight = (await card.boundingBox()).height;
  expect(cardHeight).toBeLessThan(900);

  const list = page.getByTestId('chat-messages');
  const overflows = await list.evaluate(
    (node) => node.scrollHeight > node.clientHeight + 4);
  expect(overflows, 'forty messages did not overflow the list').toBe(true);

  // And it is scrolled to the newest, which is what a chat is for.
  const atBottom = await list.evaluate(
    (node) => node.scrollTop + node.clientHeight >= node.scrollHeight - 8);
  expect(atBottom).toBe(true);
});


test('FLOW F: no chat card before a Broadcast exists', async ({ page }) => {
  await mockBackend(page, { stores: STORES });
  await page.goto('/console');

  await expect(page.getByTestId('chat-card')).toBeVisible();
  await expect(page.getByTestId('chat-card')).toContainText('Chat opens with the Broadcast');
  await expect(page.getByTestId('chat-input')).toHaveCount(0);
});
