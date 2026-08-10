import { strict as assert } from 'node:assert';

import { parseReactionRequest } from './reaction.js';

{
  const parsed = parseReactionRequest({
    chatId: '12345@g.us',
    messageId: 'message-1',
    emoji: '🙌',
    fromMe: false,
    participant: '67890@s.whatsapp.net',
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
        },
      },
    },
  });
}

{
  const parsed = parseReactionRequest({
    chatId: '12345@s.whatsapp.net',
    messageId: 'message-2',
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
  [{ messageId: 'message-1', emoji: '🙌' }, 'chatId is required'],
  [{ chatId: '12345@s.whatsapp.net', emoji: '🙌' }, 'messageId is required'],
  [{ chatId: '12345@s.whatsapp.net', messageId: 'message-1' }, 'emoji is required'],
  [{ chatId: '12345@s.whatsapp.net', messageId: 'message-1', emoji: '🙌\nnope' }, 'emoji must not contain line breaks'],
  [{ chatId: '12345@s.whatsapp.net', messageId: 'message-1', emoji: 'x'.repeat(33) }, 'emoji must be at most 32 characters'],
]) {
  assert.deepStrictEqual(parseReactionRequest(body), { ok: false, error });
}

console.log('✅ Reaction request tests passed.');
