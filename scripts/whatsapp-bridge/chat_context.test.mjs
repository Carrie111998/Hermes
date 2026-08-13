import test from 'node:test';
import assert from 'node:assert/strict';

import {
  captureUpsertChatContext,
  createRecentChatContextStore,
  installChatContextRoute,
  readRecentChatContext,
} from './chat_context.js';

test('recent chat context stays bounded per direct chat', () => {
  const store = createRecentChatContextStore({ maxEntriesPerChat: 2 });
  const chatId = '15551234567@s.whatsapp.net';

  store.record(chatId, { messageId: 'm1', body: 'first', fromMe: false });
  store.record(chatId, { messageId: 'm2', body: 'second', fromMe: true });
  store.record(chatId, { messageId: 'm3', body: 'third', fromMe: false });

  assert.deepEqual(store.recent(chatId, 50), [
    { messageId: 'm2', text: 'second', fromMe: true, mediaUrls: [] },
    { messageId: 'm3', text: 'third', fromMe: false, mediaUrls: [] },
  ]);
});

test('recent chat context is isolated by exact chat ID', () => {
  const store = createRecentChatContextStore();
  const phoneChat = '15551234567@s.whatsapp.net';
  const lidChat = '15551234567@lid';

  store.record(phoneChat, { messageId: 'phone-1', body: 'phone chat', fromMe: false });
  store.record(lidChat, { messageId: 'lid-1', body: 'lid chat', fromMe: true });

  assert.deepEqual(store.recent(phoneChat, 50).map(({ messageId }) => messageId), ['phone-1']);
  assert.deepEqual(store.recent(lidChat, 50).map(({ messageId }) => messageId), ['lid-1']);
  assert.deepEqual(store.recent('15550000000@s.whatsapp.net', 50), []);
});

test('recent chat context exposes only sanitized transcript fields', () => {
  const store = createRecentChatContextStore();
  const chatId = '15551234567@s.whatsapp.net';

  store.record(chatId, {
    messageId: 'image-1',
    body: 'new product',
    fromMe: false,
    mediaType: 'image',
    timestamp: { toNumber: () => 1723456789, credentials: 'must not leak' },
    mediaUrls: [
      '/tmp/hermes-whatsapp/images/img_image-1.jpg',
      'https://mmg.whatsapp.net/signed?token=secret',
      42,
    ],
    rawMessage: { credentials: 'must not leak' },
    senderId: 'private@s.whatsapp.net',
  });

  assert.deepEqual(store.recent(chatId, 1), [{
    messageId: 'image-1',
    text: 'new product',
    fromMe: false,
    mediaType: 'image',
    timestamp: 1723456789,
    mediaUrls: ['/tmp/hermes-whatsapp/images/img_image-1.jpg'],
  }]);
});

test('chat context requests validate exact direct JIDs and bounded integer limits', () => {
  const store = createRecentChatContextStore();
  const chatId = '15551234567@s.whatsapp.net';
  store.record(chatId, { messageId: 'm1', body: 'hello', fromMe: false });

  assert.deepEqual(readRecentChatContext(store, chatId, undefined), {
    status: 200,
    body: { chatId, messages: [{ messageId: 'm1', text: 'hello', fromMe: false, mediaUrls: [] }] },
  });
  assert.deepEqual(readRecentChatContext(store, chatId, '999'), {
    status: 200,
    body: { chatId, messages: [{ messageId: 'm1', text: 'hello', fromMe: false, mediaUrls: [] }] },
  });

  for (const invalidChatId of [
    '120363001234567890@g.us',
    'status@broadcast',
    '15551234567@s.whatsapp.net@evil',
    '15551234567%40s.whatsapp.net',
  ]) {
    assert.equal(readRecentChatContext(store, invalidChatId, '10').status, 400);
  }
  for (const invalidLimit of ['0', '-1', '1.5', '10x', '', ['1', '2']]) {
    assert.equal(readRecentChatContext(store, chatId, invalidLimit).status, 400);
  }
});

test('messages.upsert capture retains downloaded inbound images before ingress gating', () => {
  const store = createRecentChatContextStore();
  const chatId = '15551234567@s.whatsapp.net';

  captureUpsertChatContext(store, {
    messageId: 'customer-image-1',
    chatId,
    body: 'new item',
    mediaType: 'image',
    mediaUrls: ['/tmp/hermes-whatsapp/images/customer-image-1.jpg'],
    timestamp: 1723456790,
  }, { key: { fromMe: false } });

  assert.deepEqual(store.recent(chatId, 1), [{
    messageId: 'customer-image-1',
    text: 'new item',
    fromMe: false,
    mediaType: 'image',
    timestamp: 1723456790,
    mediaUrls: ['/tmp/hermes-whatsapp/images/customer-image-1.jpg'],
  }]);
});

test('HTTP route returns context without requiring a connected socket', () => {
  const routes = new Map();
  const app = { get(routePath, handler) { routes.set(routePath, handler); } };
  const store = createRecentChatContextStore();
  const chatId = '15551234567@s.whatsapp.net';
  store.record(chatId, { messageId: 'm1', body: 'hello', fromMe: false });
  installChatContextRoute(app, store);

  const response = { statusCode: 200, body: null };
  const res = {
    status(code) { response.statusCode = code; return this; },
    json(body) { response.body = body; return this; },
  };
  routes.get('/chat-context/:chatId')({ params: { chatId }, query: { limit: '1' } }, res);

  assert.equal(response.statusCode, 200);
  assert.deepEqual(response.body, {
    chatId,
    messages: [{ messageId: 'm1', text: 'hello', fromMe: false, mediaUrls: [] }],
  });
});
