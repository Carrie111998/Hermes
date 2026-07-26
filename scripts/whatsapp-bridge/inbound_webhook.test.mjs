import test from 'node:test';
import assert from 'node:assert/strict';

import { createInboundWebhook, resolveSenderPhone } from './inbound_webhook.js';

function jsonResponse(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  };
}

function makeWebhook(overrides = {}) {
  const calls = { fetches: [], unlinked: [] };
  let clock = 0;
  const webhook = createInboundWebhook({
    url: 'https://backend.example/inbound',
    numbersUrl: 'https://backend.example/numbers',
    secret: 's3cret',
    fetchImpl: async (target, options = {}) => {
      calls.fetches.push({ target, options });
      return overrides.respond
        ? overrides.respond(target, options)
        : jsonResponse({ tails: ['501111111'] });
    },
    now: () => clock,
    readFile: overrides.readFile || (() => Buffer.from('media-bytes')),
    unlink: (filePath) => calls.unlinked.push(filePath),
    log: { warn: () => {} },
    ...overrides.config,
  });
  return { webhook, calls, tick: (ms) => { clock += ms; } };
}

test('resolveSenderPhone prefers senderPn over the chat id', () => {
  const msg = { key: { senderPn: '972501111111@s.whatsapp.net' } };
  assert.equal(resolveSenderPhone(msg, '123456@lid', {}), '972501111111');
});

test('resolveSenderPhone maps a LID through the session map', () => {
  assert.equal(resolveSenderPhone({}, '123456@lid', { 123456: '972502222222' }), '972502222222');
});

test('resolveSenderPhone refuses to guess for an unmapped LID', () => {
  assert.equal(resolveSenderPhone({}, '123456@lid', {}), '');
});

test('resolveSenderPhone strips a device suffix from a phone JID', () => {
  assert.equal(resolveSenderPhone({}, '972503333333:12@s.whatsapp.net', {}), '972503333333');
});

test('disabled without full configuration', async () => {
  const webhook = createInboundWebhook({ url: 'https://x', numbersUrl: '', secret: 'k' });
  assert.equal(webhook.enabled, false);
  assert.equal(await webhook.shouldForward('972501111111'), false);
});

test('a sender matching a tail is forwarded', async () => {
  const { webhook } = makeWebhook();
  await webhook.refresh();
  assert.equal(await webhook.shouldForward('972501111111'), true);
  assert.equal(await webhook.shouldForward('972509999999'), false);
});

test('a miss refreshes at most once per cooldown window', async () => {
  const { webhook, calls, tick } = makeWebhook();
  assert.equal(await webhook.shouldForward('972501111111'), true);
  assert.equal(calls.fetches.length, 1);

  assert.equal(await webhook.shouldForward('972509999999'), false);
  assert.equal(calls.fetches.length, 1, 'second miss inside the cooldown must not refetch');

  tick(10001);
  assert.equal(await webhook.shouldForward('972509999999'), false);
  assert.equal(calls.fetches.length, 2, 'a miss after the cooldown refreshes again');
});

test('a failed numbers fetch keeps the previous list', async () => {
  let failNext = false;
  const { webhook, tick } = makeWebhook({
    respond: () => {
      if (failNext) {
        throw new Error('down');
      }
      return jsonResponse({ tails: ['501111111'] });
    },
  });
  await webhook.refresh();
  failNext = true;
  tick(60000);
  await webhook.refresh();
  assert.equal(await webhook.shouldForward('972501111111'), true);
});

test('forward posts the message with the shared secret and stores media as base64', async () => {
  const { webhook, calls } = makeWebhook({
    respond: (target) => (target.endsWith('/inbound')
      ? jsonResponse({ ok: true })
      : jsonResponse({ tails: [] })),
  });
  const result = await webhook.forward({
    from: '972501111111',
    text: 'הכל נכון',
    mediaUrls: ['/cache/img_1.jpg'],
    mime: 'image/jpeg',
  });
  assert.equal(result.stored, true);

  const post = calls.fetches.find((call) => call.target.endsWith('/inbound'));
  assert.equal(post.options.method, 'POST');
  assert.equal(post.options.headers['x-hermes-key'], 's3cret');
  const body = JSON.parse(post.options.body);
  assert.equal(body.from, '972501111111');
  assert.equal(body.text, 'הכל נכון');
  assert.deepEqual(body.attachments, [{
    name: 'img_1.jpg',
    contentType: 'image/jpeg',
    contentB64: Buffer.from('media-bytes').toString('base64'),
  }]);
  assert.deepEqual(calls.unlinked, ['/cache/img_1.jpg'], 'media leaves the cache after forwarding');
});

test('a 404 means no open conversation, not an error, and media is still cleaned up', async () => {
  const { webhook, calls } = makeWebhook({
    respond: (target) => (target.endsWith('/inbound')
      ? jsonResponse({ error: 'no open outreach' }, 404)
      : jsonResponse({ tails: [] })),
  });
  const result = await webhook.forward({ from: '972501111111', text: 'hi', mediaUrls: ['/cache/a.jpg'] });
  assert.equal(result.stored, false);
  assert.deepEqual(calls.unlinked, ['/cache/a.jpg']);
});

test('a server error throws and leaves media on disk for the logs', async () => {
  const { webhook, calls } = makeWebhook({
    respond: (target) => (target.endsWith('/inbound')
      ? jsonResponse({}, 500)
      : jsonResponse({ tails: [] })),
  });
  await assert.rejects(
    () => webhook.forward({ from: '972501111111', text: 'hi', mediaUrls: ['/cache/a.jpg'] }),
    /inbound webhook returned 500/,
  );
  assert.deepEqual(calls.unlinked, []);
});

test('an oversized attachment is skipped, the text still goes through', async () => {
  const { webhook, calls } = makeWebhook({
    config: { maxAttachmentBytes: 4 },
    respond: (target) => (target.endsWith('/inbound')
      ? jsonResponse({ ok: true })
      : jsonResponse({ tails: [] })),
  });
  await webhook.forward({ from: '972501111111', text: 'hi', mediaUrls: ['/cache/big.jpg'] });
  const post = calls.fetches.find((call) => call.target.endsWith('/inbound'));
  assert.deepEqual(JSON.parse(post.options.body).attachments, []);
});

test('an unreadable media file degrades to a text-only forward', async () => {
  const { webhook, calls } = makeWebhook({
    readFile: () => { throw new Error('gone'); },
    respond: (target) => (target.endsWith('/inbound')
      ? jsonResponse({ ok: true })
      : jsonResponse({ tails: [] })),
  });
  const result = await webhook.forward({ from: '972501111111', text: 'hi', mediaUrls: ['/cache/x.jpg'] });
  assert.equal(result.stored, true);
  const post = calls.fetches.find((call) => call.target.endsWith('/inbound'));
  assert.deepEqual(JSON.parse(post.options.body).attachments, []);
});
