function requiredString(value, field) {
  if (typeof value !== 'string' || !value.trim()) {
    return { ok: false, error: `${field} is required` };
  }
  return { ok: true, value: value.trim() };
}

export function parseReactionRequest(body) {
  const input = body && typeof body === 'object' && !Array.isArray(body) ? body : {};
  if (!input.key || typeof input.key !== 'object' || Array.isArray(input.key)) {
    return { ok: false, error: 'key is required' };
  }
  const chatId = requiredString(input.key.remoteJid, 'key.remoteJid');
  if (!chatId.ok) return chatId;

  const messageId = requiredString(input.key.id, 'key.id');
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

  return {
    ok: true,
    chatId: chatId.value,
    payload: {
      react: {
        text: input.emoji,
        // Baileys message keys may carry addressing fields beyond the common
        // four. Preserve the exact inbound key so LID/group reactions target
        // the original message reliably.
        key: input.key,
      },
    },
  };
}
