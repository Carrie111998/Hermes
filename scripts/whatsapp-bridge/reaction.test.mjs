import { strict as assert } from 'node:assert';

import { parseReactionRequest } from './reaction.js';

{
  const parsed = parseReactionRequest({
    key: {
      remoteJid: '12345@g.us',
      id: 'message-1',
      fromMe: false,
      participant: '67890@s.whatsapp.net',
      addressingMode: 'lid',
    },
    emoji: '🙌',
  });

  assert.deepStrictEqual(parsed, {
    ok: true,
    chatId: '12345@g.us',
    payload: {
      react: {
        text: '🙌',
        key: {
          remoteJid: '12345@g.us',
          id: 'message-1',
          fromMe: false,
          participant: '67890@s.whatsapp.net',
          addressingMode: 'lid',
        },
      },
    },
  });
}

{
  const parsed = parseReactionRequest({
    key: {
      remoteJid: '12345@s.whatsapp.net',
      id: 'message-2',
      fromMe: false,
    },
    emoji: '',
  });

  assert.deepStrictEqual(parsed, {
    ok: true,
    chatId: '12345@s.whatsapp.net',
    payload: {
      react: {
        text: '',
        key: {
          remoteJid: '12345@s.whatsapp.net',
          id: 'message-2',
          fromMe: false,
        },
      },
    },
  });
}

for (const [body, error] of [
  [{ emoji: '🙌' }, 'key is required'],
  [{ key: { id: 'message-1' }, emoji: '🙌' }, 'key.remoteJid is required'],
  [{ key: { remoteJid: '12345@s.whatsapp.net' }, emoji: '🙌' }, 'key.id is required'],
  [{ key: { remoteJid: '12345@s.whatsapp.net', id: 'message-1' } }, 'emoji is required'],
  [{ key: { remoteJid: '12345@s.whatsapp.net', id: 'message-1' }, emoji: '🙌\nnope' }, 'emoji must not contain line breaks'],
  [{ key: { remoteJid: '12345@s.whatsapp.net', id: 'message-1' }, emoji: 'x'.repeat(33) }, 'emoji must be at most 32 characters'],
]) {
  assert.deepStrictEqual(parseReactionRequest(body), { ok: false, error });
}

console.log('✅ Reaction request tests passed.');
