function requiredString(value, field) {
  if (typeof value !== 'string' || !value.trim()) {
    return { ok: false, error: `${field} is required` };
  }
  return { ok: true, value: value.trim() };
}

export function parseReactionRequest(body) {
  const input = body && typeof body === 'object' && !Array.isArray(body) ? body : {};
  const chatId = requiredString(input.chatId, 'chatId');
  if (!chatId.ok) return chatId;

  const messageId = requiredString(input.messageId, 'messageId');
  if (!messageId.ok) return messageId;

  if (typeof input.emoji !== 'string') {
    return { ok: false, error: 'emoji is required' };
  }
  if (/\r|\n/.test(input.emoji)) {
    return { ok: false, error: 'emoji must not contain line breaks' };
  }
  if (input.emoji.length > 32) {
    return { ok: false, error: 'emoji must be at most 32 characters' };
  }

  const participant = typeof input.participant === 'string' ? input.participant.trim() : '';
  return {
    ok: true,
    chatId: chatId.value,
    payload: {
      react: {
        text: input.emoji,
        key: {
          remoteJid: chatId.value,
          id: messageId.value,
          fromMe: input.fromMe === true,
          ...(participant ? { participant } : {}),
        },
      },
    },
  };
}
