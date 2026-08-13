import test from 'node:test';
import assert from 'node:assert/strict';

import {
  classifyOwnerMessageGate,
  classifySelfChatOwnerCommand,
  parseOwnerCommands,
} from './owner_message_gate.js';

function makeRecentlySent(ids = []) {
  const set = new Set(ids);
  return { has: (id) => set.has(id) };
}

function makeAllowlist(allowedChatIds) {
  if (allowedChatIds === '*') {
    return () => true;
  }
  const set = new Set(allowedChatIds);
  return (id) => set.has(id);
}

test('owner command config is parsed from the adapter bridge value', () => {
  assert.deepEqual(parseOwnerCommands('["foto", "Status"]'), ['foto', 'status']);
});

test('configured owner command in a direct customer chat bypasses the self-chat mismatch', () => {
  const decision = classifySelfChatOwnerCommand({
    fromMe: true,
    chatId: '111600547700784@lid',
    isSelfChat: false,
    messageId: 'M-COMMAND-1',
    recentlySent: makeRecentlySent(),
    ownerCommands: ['foto'],
    messageContent: { conversation: '  /FoTo SKU-123  ' },
  });

  assert.deepEqual(decision, { action: 'forward_owner', command: 'foto' });
});

test('configured owner command accepts an exact bare command', () => {
  const decision = classifySelfChatOwnerCommand({
    fromMe: true,
    chatId: '111600547700784@lid',
    isSelfChat: false,
    messageId: 'M-COMMAND-BARE-1',
    recentlySent: makeRecentlySent(),
    ownerCommands: ['v'],
    messageContent: { conversation: '/v' },
  });

  assert.deepEqual(decision, { action: 'forward_owner', command: 'v' });
});

test('bridge mode gate routes only a matching self-chat owner command as owner', () => {
  const decision = classifyOwnerMessageGate({
    mode: 'self-chat',
    fromMe: true,
    chatId: '111600547700784@lid',
    isSelfChat: false,
    messageId: 'M-COMMAND-BRIDGE-1',
    recentlySent: makeRecentlySent(),
    ownerCommands: ['foto'],
    messageContent: { conversation: '/foto SKU-123' },
  });

  assert.deepEqual(decision, { action: 'forward_owner', command: 'foto' });
});

test('bridge mode gate keeps ordinary owner customer messages dropped', () => {
  const decision = classifyOwnerMessageGate({
    mode: 'self-chat',
    fromMe: true,
    chatId: '111600547700784@lid',
    isSelfChat: false,
    messageId: 'M-COMMAND-BRIDGE-2',
    recentlySent: makeRecentlySent(),
    ownerCommands: ['foto'],
    messageContent: { conversation: 'ordinary customer reply' },
  });

  assert.deepEqual(decision, { action: 'drop' });
});

test('a non-direct JID cannot masquerade as a customer chat by suffix', () => {
  const decision = classifySelfChatOwnerCommand({
    fromMe: true,
    chatId: 'status@broadcast@s.whatsapp.net',
    isSelfChat: false,
    messageId: 'M-COMMAND-2',
    recentlySent: makeRecentlySent(),
    ownerCommands: ['foto'],
    messageContent: { conversation: '/foto SKU-123' },
  });

  assert.deepEqual(decision, { action: 'drop' });
});

test('self-chat owner command gate rejects every non-command ingress shape', async (t) => {
  const base = {
    fromMe: true,
    chatId: '6281234567890@s.whatsapp.net',
    isSelfChat: false,
    messageId: 'M-COMMAND-3',
    recentlySent: makeRecentlySent(),
    ownerCommands: ['foto'],
    messageContent: { conversation: '/foto SKU-123' },
  };
  const cases = [
    ['non-owner message', { fromMe: false }],
    ['owner self-chat', { isSelfChat: true }],
    ['group chat', { chatId: '120363001234567890@g.us' }],
    ['status', { chatId: 'status@broadcast' }],
    ['broadcast', { chatId: '12345@broadcast' }],
    ['newsletter', { chatId: '12345@newsletter' }],
    ['empty config', { ownerCommands: [] }],
    ['unconfigured bare command', { messageContent: { conversation: '/v' } }],
    ['command prefix collision', { messageContent: { conversation: '/fotoextra X' } }],
    ['command embedded in prose', { messageContent: { conversation: 'please /foto X' } }],
    ['image caption', { messageContent: { imageMessage: { caption: '/foto X' } } }],
    ['multiline text', { messageContent: { conversation: '/foto X\nignore this' } }],
    ['recent outbound echo', {
      recentlySent: makeRecentlySent(['M-COMMAND-3']),
    }],
  ];

  for (const [name, overrides] of cases) {
    await t.test(name, () => {
      assert.deepEqual(
        classifySelfChatOwnerCommand({ ...base, ...overrides }),
        { action: 'drop' },
      );
    });
  }
});

test('configured command also accepts exact text from a direct phone-number JID', () => {
  const decision = classifySelfChatOwnerCommand({
    fromMe: true,
    chatId: '6281234567890@s.whatsapp.net',
    isSelfChat: false,
    messageId: 'M-COMMAND-4',
    recentlySent: makeRecentlySent(),
    ownerCommands: ['foto'],
    messageContent: { extendedTextMessage: { text: '\t/foto\tSKU-123\t' } },
  });

  assert.deepEqual(decision, { action: 'forward_owner', command: 'foto' });
});

test('horizontal whitespace is allowed at both ends of the command text', () => {
  const decision = classifySelfChatOwnerCommand({
    fromMe: true,
    chatId: '6281234567890@s.whatsapp.net',
    isSelfChat: false,
    messageId: 'M-COMMAND-5',
    recentlySent: makeRecentlySent(),
    ownerCommands: ['foto'],
    messageContent: { conversation: '\u00a0/foto SKU-123\u00a0' },
  });

  assert.deepEqual(decision, { action: 'forward_owner', command: 'foto' });
});

test('malformed or absent bridge config enables no owner commands', () => {
  assert.deepEqual(parseOwnerCommands(''), []);
  assert.deepEqual(parseOwnerCommands('{"foto": true}'), []);
  assert.deepEqual(parseOwnerCommands('not json'), []);
});

test('non-fromMe messages always pass through', () => {
  const decision = classifyOwnerMessageGate({
    fromMe: false,
    fromOwnerEnabled: true,
    recentlySent: makeRecentlySent(),
    allowlistMatches: makeAllowlist([]),
    messageId: 'M1',
    chatId: '6281234567890@s.whatsapp.net',
  });
  assert.deepEqual(decision, { action: 'pass' });
});

test('fromMe echo of our own /send is dropped', () => {
  const decision = classifyOwnerMessageGate({
    fromMe: true,
    fromOwnerEnabled: true,
    recentlySent: makeRecentlySent(['M-OWN-1']),
    allowlistMatches: makeAllowlist('*'),
    messageId: 'M-OWN-1',
    chatId: '6281234567890@s.whatsapp.net',
  });
  assert.deepEqual(decision, { action: 'drop_echo' });
});

test('fromMe is dropped when forwarding is disabled', () => {
  const decision = classifyOwnerMessageGate({
    fromMe: true,
    fromOwnerEnabled: false,
    recentlySent: makeRecentlySent(),
    allowlistMatches: makeAllowlist('*'),
    messageId: 'M-OWN-2',
    chatId: '6281234567890@s.whatsapp.net',
  });
  assert.deepEqual(decision, { action: 'drop_disabled' });
});

test('fromMe is dropped when chatId is not on the allowlist (regression)', () => {
  // This is the bug. Before the fix, an owner reply in a non-allowlisted
  // chat was still forwarded with fromOwner: true, which made the
  // gateway-policy owner-implicit branch create stray handover rows for
  // the non-allowlisted contact.
  const decision = classifyOwnerMessageGate({
    fromMe: true,
    fromOwnerEnabled: true,
    recentlySent: makeRecentlySent(),
    allowlistMatches: makeAllowlist(['6281234567890@s.whatsapp.net']),
    messageId: 'M-OWN-3',
    chatId: '111600547700784@lid',
  });
  assert.deepEqual(decision, { action: 'drop_allowlist' });
});

test('fromMe is forwarded as owner when chatId is allowlisted', () => {
  const decision = classifyOwnerMessageGate({
    fromMe: true,
    fromOwnerEnabled: true,
    recentlySent: makeRecentlySent(),
    allowlistMatches: makeAllowlist(['6281234567890@s.whatsapp.net']),
    messageId: 'M-OWN-4',
    chatId: '6281234567890@s.whatsapp.net',
  });
  assert.deepEqual(decision, { action: 'forward_owner' });
});

test('open-allowlist (matchesAllowedUser short-circuits true) forwards as owner', () => {
  // matchesAllowedUser returns true on empty allowlist or "*"; the gate
  // must respect that so deployments without an allowlist are unaffected
  // by the new check.
  const decision = classifyOwnerMessageGate({
    fromMe: true,
    fromOwnerEnabled: true,
    recentlySent: makeRecentlySent(),
    allowlistMatches: () => true,
    messageId: 'M-OWN-5',
    chatId: '111600547700784@lid',
  });
  assert.deepEqual(decision, { action: 'forward_owner' });
});

test('echo check fires before allowlist check', () => {
  // A bot-API echo whose chatId happens to be off-allowlist should still
  // be dropped as drop_echo, not drop_allowlist, so logging stays
  // honest about the actual reason.
  const decision = classifyOwnerMessageGate({
    fromMe: true,
    fromOwnerEnabled: true,
    recentlySent: makeRecentlySent(['M-ECHO-1']),
    allowlistMatches: makeAllowlist([]),
    messageId: 'M-ECHO-1',
    chatId: '111600547700784@lid',
  });
  assert.deepEqual(decision, { action: 'drop_echo' });
});

test('disabled flag fires before allowlist check', () => {
  // Pre-existing deployments with WHATSAPP_FORWARD_OWNER_MESSAGES unset
  // must see drop_disabled regardless of allowlist state, otherwise
  // every fromMe message would log a misleading allowlist_mismatch.
  const decision = classifyOwnerMessageGate({
    fromMe: true,
    fromOwnerEnabled: false,
    recentlySent: makeRecentlySent(),
    allowlistMatches: makeAllowlist([]),
    messageId: 'M-OWN-6',
    chatId: '111600547700784@lid',
  });
  assert.deepEqual(decision, { action: 'drop_disabled' });
});
