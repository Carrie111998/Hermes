import path from 'node:path';

export function isDirectChatId(chatId) {
  return typeof chatId === 'string' && /^[0-9]+@(lid|s\.whatsapp\.net)$/u.test(chatId);
}

const MAX_QUERY_LIMIT = 50;

export function readRecentChatContext(store, chatId, rawLimit) {
  if (!isDirectChatId(chatId)) {
    return { status: 400, body: { error: 'chatId must be a direct WhatsApp JID' } };
  }

  let limit = MAX_QUERY_LIMIT;
  if (rawLimit !== undefined) {
    if (typeof rawLimit !== 'string' || !/^[1-9][0-9]*$/u.test(rawLimit)) {
      return { status: 400, body: { error: 'limit must be a positive integer' } };
    }
    limit = Math.min(Number(rawLimit), MAX_QUERY_LIMIT);
  }

  return {
    status: 200,
    body: { chatId, messages: store.recent(chatId, limit) },
  };
}

export function captureUpsertChatContext(store, event, msg) {
  if (!event || !msg?.key) return false;
  return store.record(event.chatId, {
    ...event,
    fromMe: msg.key.fromMe === true,
  });
}

export function installChatContextRoute(app, store) {
  app.get('/chat-context/:chatId', (req, res) => {
    const result = readRecentChatContext(store, req.params.chatId, req.query.limit);
    return res.status(result.status).json(result.body);
  });
}

export function createRecentChatContextStore({ maxEntriesPerChat = 50 } = {}) {
  const maxEntries = Math.max(1, Number.isSafeInteger(maxEntriesPerChat) ? maxEntriesPerChat : 50);
  const chats = new Map();

  return {
    record(chatId, event) {
      if (!isDirectChatId(chatId) || !event || typeof event !== 'object') return false;
      const entries = chats.get(chatId) || [];
      const entry = {
        messageId: typeof event.messageId === 'string' ? event.messageId : '',
        text: typeof event.body === 'string' ? event.body : '',
        fromMe: event.fromMe === true,
        mediaUrls: Array.isArray(event.mediaUrls)
          ? event.mediaUrls.filter((url) => typeof url === 'string' && path.isAbsolute(url))
          : [],
      };
      if (typeof event.mediaType === 'string' && event.mediaType) {
        entry.mediaType = event.mediaType;
      }
      const timestamp = typeof event.timestamp === 'number'
        ? event.timestamp
        : event.timestamp?.toNumber?.();
      if (typeof timestamp === 'number' && Number.isFinite(timestamp)) {
        entry.timestamp = timestamp;
      }
      entries.push(entry);
      if (entries.length > maxEntries) entries.splice(0, entries.length - maxEntries);
      chats.set(chatId, entries);
      return true;
    },

    recent(chatId, limit = maxEntries) {
      if (!isDirectChatId(chatId)) return null;
      const boundedLimit = Math.min(maxEntries, Math.max(1, Number.isSafeInteger(limit) ? limit : maxEntries));
      return (chats.get(chatId) || []).slice(-boundedLimit).map((entry) => ({ ...entry }));
    },
  };
}
