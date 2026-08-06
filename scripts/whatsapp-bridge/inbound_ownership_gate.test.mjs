import assert from 'node:assert/strict';
import test from 'node:test';

import {
  applyInboundOwnershipGate,
  buildOwnershipSignal,
  normalizedPrefixes,
} from './inbound_ownership_gate.js';

const baseEvent = {
  messageId: 'current-1',
  body: '1',
  quotedMessageId: 'quoted-1',
  fromOwner: false,
};

function config(overrides = {}) {
  return {
    url: 'https://router.invalid/classify',
    token: 'test-token',
    timeoutMs: 50,
    prefixes: ['jeffersom'],
    ...overrides,
  };
}

test('disabled router preserves existing behavior', async () => {
  const result = await applyInboundOwnershipGate({
    config: null,
    event: baseEvent,
    senderAliases: ['5511999999999'],
    fetchFn: async () => { throw new Error('must not call'); },
  });
  assert.deepEqual(result, { action: 'pass', reason: 'router_disabled' });
});

test('rejects insecure remote router URLs without sending the shared secret', async () => {
  let called = false;
  const result = await applyInboundOwnershipGate({
    config: config({ url: 'http://example.com/classify' }),
    event: baseEvent,
    senderAliases: ['5511999999999'],
    fetchFn: async () => { called = true; },
  });
  assert.equal(called, false);
  assert.deepEqual(result, { action: 'drop', reason: 'invalid_router_url' });
});

test('missing router token fails closed without sending metadata', async () => {
  let called = false;
  const result = await applyInboundOwnershipGate({
    config: config({ token: '' }),
    event: baseEvent,
    senderAliases: ['5511999999999'],
    fetchFn: async () => {
      called = true;
      return { ok: true, json: async () => ({ owner: 'jeffersom' }) };
    },
  });
  assert.equal(called, false);
  assert.deepEqual(result, { action: 'drop', reason: 'missing_router_token' });
});

test('allows loopback HTTP router URLs without weakening remote transport', async () => {
  let calls = 0;
  const result = await applyInboundOwnershipGate({
    config: config({ url: 'http://[::1]/classify' }),
    event: baseEvent,
    senderAliases: ['5511999999999'],
    fetchFn: async () => {
      calls += 1;
      return { ok: true, json: async () => ({ owner: 'jeffersom' }) };
    },
  });
  assert.deepEqual(result, { action: 'pass', reason: 'owned_by_jeffersom' });
  assert.equal(calls, 1);
});

test('Jeffersom prefix bypasses router even during AutoCria flow', async () => {
  let called = false;
  const result = await applyInboundOwnershipGate({
    config: config(),
    event: { body: '  JeFfErSoM, preciso de ajuda', quotedMessageId: 'auto-1' },
    senderAliases: ['5511999999999'],
    fetchFn: async () => { called = true; throw new Error('must not call'); },
  });
  assert.equal(called, false);
  assert.deepEqual(result, { action: 'pass', reason: 'explicit_agent_prefix' });
});

test('Jeffersom remains reserved when custom prefixes are configured', async () => {
  let called = false;
  const result = await applyInboundOwnershipGate({
    config: config({ prefixes: ['assistant'] }),
    event: { ...baseEvent, body: 'Jeffersom, status' },
    senderAliases: ['5511999999999'],
    fetchFn: async () => { called = true; throw new Error('must not call'); },
  });
  assert.equal(called, false);
  assert.deepEqual(result, { action: 'pass', reason: 'explicit_agent_prefix' });
});

test('reserved prefix has priority without truncating the eighth custom prefix', () => {
  const custom = Array.from({ length: 8 }, (_, index) => `custom-${index + 1}`);
  assert.deepEqual(normalizedPrefixes(custom), ['jeffersom', ...custom]);
});

test('builds metadata-only signal without the message body', () => {
  const signal = buildOwnershipSignal({
    event: { messageId: 'current-2', body: 'private edited answer', quotedMessageId: 'q-1', fromOwner: true },
    senderAliases: ['5511999999999', '123456789@lid'],
  });
  assert.deepEqual(signal, {
    senderAliases: ['5511999999999', '123456789@lid'],
    messageId: 'current-2',
    quotedMessageId: 'q-1',
    replyKind: 'text',
    hasText: true,
    fromOwner: true,
  });
  assert.equal('body' in signal, false);
  assert.equal('text' in signal, false);
});

test('classifies 1 and 2 as approval signals', () => {
  assert.equal(buildOwnershipSignal({ event: { body: '1' }, senderAliases: [] }).replyKind, 'approval_1');
  assert.equal(buildOwnershipSignal({ event: { body: ' 2 ' }, senderAliases: [] }).replyKind, 'approval_2');
});

test('AutoCria ownership drops before agent dispatch', async () => {
  const result = await applyInboundOwnershipGate({
    config: config(),
    event: baseEvent,
    senderAliases: ['5511999999999'],
    fetchFn: async (_url, init) => {
      const payload = JSON.parse(init.body);
      assert.equal('body' in payload, false);
      assert.equal(init.headers.authorization, 'Bearer test-token');
      assert.equal(init.redirect, 'error');
      return { ok: true, json: async () => ({ owner: 'autocria', reason: 'quoted_active_session' }) };
    },
  });
  assert.deepEqual(result, { action: 'drop', reason: 'owned_by_autocria' });
});

test('Jeffersom ownership passes to agent dispatch', async () => {
  const result = await applyInboundOwnershipGate({
    config: config(),
    event: { body: 'status', quotedMessageId: null },
    senderAliases: ['5511999999999'],
    fetchFn: async () => ({ ok: true, json: async () => ({ owner: 'jeffersom', reason: 'no_active_session' }) }),
  });
  assert.deepEqual(result, { action: 'pass', reason: 'owned_by_jeffersom' });
});

test('ambiguous, HTTP failure, network error, and timeout fail closed', async () => {
  const scenarios = [
    async () => ({ ok: true, json: async () => ({ owner: 'ambiguous', reason: 'multiple_sessions' }) }),
    async () => ({ ok: false, status: 503, json: async () => ({}) }),
    async () => { throw new Error('network'); },
    async (_url, init) => await new Promise((_resolve, reject) => {
      init.signal.addEventListener('abort', () => reject(Object.assign(new Error('timeout'), { name: 'AbortError' })));
    }),
  ];
  for (const fetchFn of scenarios) {
    const result = await applyInboundOwnershipGate({
      config: config({ timeoutMs: 5 }),
      event: baseEvent,
      senderAliases: ['5511999999999'],
      fetchFn,
    });
    assert.equal(result.action, 'drop');
  }
});
