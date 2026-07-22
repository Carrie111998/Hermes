const DEFAULT_REFRESH_TTL_MS = 5 * 60 * 1000;
const DEFAULT_REFRESH_EVERY_SENDS = 20;

function stableDeviceList(value) {
  if (!Array.isArray(value)) return [];
  return value
    .map((device) => ({
      user: String(device?.user || ''),
      // Baileys 6.x cache entries contain only { user, device }. The server is
      // chosen later from group addressingMode, so it must not be required to
      // detect a device-list mutation.
      server: String(device?.server || ''),
      device: Number(device?.device || 0),
    }))
    .filter((device) => device.user)
    .sort((left, right) => `${left.user}:${left.device}@${left.server}`
      .localeCompare(`${right.user}:${right.device}@${right.server}`));
}

function sameDeviceList(left, right) {
  return JSON.stringify(stableDeviceList(left)) === JSON.stringify(stableDeviceList(right));
}

function userFromJid(jid) {
  return String(jid || '').split('@')[0].split(':')[0];
}

function deviceJid(device, defaultServer = '') {
  if (device?.jid) return String(device.jid);
  const suffix = Number(device?.device || 0) > 0 ? `:${Number(device.device)}` : '';
  const server = String(device?.server || defaultServer || '');
  return server ? `${device?.user || ''}${suffix}@${server}` : '';
}

function isSenderKeyEligible(device, defaultServer = '') {
  const jid = deviceJid(device, defaultServer);
  return jid
    && Number(device?.device || 0) !== 99
    && !jid.endsWith('@hosted')
    && !jid.endsWith('@hosted.lid');
}

/**
 * Baileys owns this cache and mutates it when a `devices` notification lands.
 * Observing those mutations gives the bridge the missing public hook without
 * patching node_modules. The cache remains synchronous for Baileys 6.x while
 * also exposing mget/mset for 7.x.
 */
export function createObservableUserDevicesCache({ now = Date.now, ttlMs = DEFAULT_REFRESH_TTL_MS, onChange } = {}) {
  const entries = new Map();

  function liveEntry(key) {
    const entry = entries.get(String(key));
    if (!entry) return undefined;
    if (entry.expiresAt <= now()) {
      entries.delete(String(key));
      return undefined;
    }
    return entry;
  }

  function notify(change) {
    if (typeof onChange === 'function') onChange(change);
  }

  return {
    get(key) {
      return liveEntry(key)?.value;
    },
    mget(keys) {
      const result = {};
      for (const key of keys || []) {
        const value = liveEntry(key)?.value;
        if (value !== undefined) result[String(key)] = value;
      }
      return result;
    },
    set(key, value) {
      const normalizedKey = String(key);
      const previous = liveEntry(normalizedKey)?.value;
      entries.set(normalizedKey, { value, expiresAt: now() + ttlMs });
      if (!sameDeviceList(previous, value)) {
        notify({ type: 'set', user: normalizedKey, previous: stableDeviceList(previous), next: stableDeviceList(value) });
      }
      return true;
    },
    mset(items) {
      for (const item of items || []) this.set(item.key, item.value);
      return true;
    },
    del(key) {
      const normalizedKey = String(key);
      const previous = liveEntry(normalizedKey)?.value;
      const deleted = entries.delete(normalizedKey);
      if (previous !== undefined) {
        notify({ type: 'del', user: normalizedKey, previous: stableDeviceList(previous), next: [] });
      }
      return deleted ? 1 : 0;
    },
    flushAll() {
      const previous = Array.from(entries.entries()).map(([user, entry]) => ({ user, value: entry.value }));
      entries.clear();
      for (const item of previous) {
        notify({ type: 'del', user: item.user, previous: stableDeviceList(item.value), next: [] });
      }
    },
    close() {
      entries.clear();
    },
  };
}

export function createSenderKeyFanoutManager({
  authKeys,
  now = Date.now,
  refreshTtlMs = DEFAULT_REFRESH_TTL_MS,
  refreshEverySends = DEFAULT_REFRESH_EVERY_SENDS,
  emit = (event) => console.log(JSON.stringify(event)),
} = {}) {
  if (!authKeys || typeof authKeys.get !== 'function' || typeof authKeys.set !== 'function') {
    throw new TypeError('authKeys with get/set is required');
  }

  const groupsByUser = new Map();
  const groupState = new Map();
  let socket = null;
  let suppressCacheObservation = 0;
  let pendingInvalidations = Promise.resolve();
  let sequence = 0;
  let deviceGeneration = 0;

  const emitEvent = (event) => emit({
    kind: 'sender-key-fanout',
    at: new Date(now()).toISOString(),
    ...event,
  });

  async function clearSenderKeyMemory(chatId, reason, details = {}) {
    await authKeys.set({ 'sender-key-memory': { [chatId]: null } });
    emitEvent({ phase: 'invalidation', chatId, reason, ...details });
  }

  function queueDeviceChange(change) {
    if (suppressCacheObservation > 0 || !change?.user) return;
    pendingInvalidations = pendingInvalidations
      .then(async () => {
        const chats = Array.from(groupsByUser.get(String(change.user)) || []).sort();
        for (const chatId of chats) {
          await clearSenderKeyMemory(chatId, 'device-list-change', {
            user: String(change.user),
            cacheMutation: change.type,
            previousDevices: change.previous,
            nextDevices: change.next,
          });
        }
      })
      .catch((error) => {
        emitEvent({ phase: 'invalidation-error', reason: 'device-list-change', error: error?.message || String(error) });
      });
  }

  const userDevicesCache = createObservableUserDevicesCache({
    now,
    ttlMs: refreshTtlMs,
    onChange: queueDeviceChange,
  });

  function noteDeviceListChange({ from = null, tag = null } = {}) {
    deviceGeneration += 1;
    suppressCacheObservation += 1;
    try {
      userDevicesCache.flushAll();
    } finally {
      suppressCacheObservation -= 1;
    }
    const knownChats = Array.from(groupState.keys()).sort();
    pendingInvalidations = pendingInvalidations
      .then(async () => {
        for (const chatId of knownChats) {
          await clearSenderKeyMemory(chatId, 'device-list-notification', {
            deviceGeneration,
            notificationFrom: from,
            notificationTag: tag,
          });
        }
      })
      .catch((error) => {
        emitEvent({ phase: 'invalidation-error', reason: 'device-list-notification', error: error?.message || String(error) });
      });
    emitEvent({
      phase: 'device-list-notification',
      deviceGeneration,
      notificationFrom: from,
      notificationTag: tag,
      knownChats,
    });
  }

  function bindSocket(nextSocket) {
    socket = nextSocket;
    socket?.ws?.on?.('CB:notification', (node) => {
      if (node?.attrs?.type !== 'devices') return;
      const child = Array.isArray(node?.content) ? node.content[0] : null;
      noteDeviceListChange({ from: node?.attrs?.from || null, tag: child?.tag || null });
    });
  }

  function trackMembership(chatId, participants) {
    for (const participant of participants) {
      const user = userFromJid(participant);
      if (!user) continue;
      const chats = groupsByUser.get(user) || new Set();
      chats.add(chatId);
      groupsByUser.set(user, chats);
    }
  }

  async function forceRefresh(chatId, participants, reason) {
    await clearSenderKeyMemory(chatId, reason);
    suppressCacheObservation += 1;
    try {
      for (const user of new Set(participants.map(userFromJid).filter(Boolean))) {
        userDevicesCache.del(user);
      }
      return await socket.getUSyncDevices(participants, false, false);
    } finally {
      suppressCacheObservation -= 1;
    }
  }

  async function prepareGroupSend(chatId, { action = 'send' } = {}) {
    if (!String(chatId || '').endsWith('@g.us')) return null;
    if (!socket || typeof socket.groupMetadata !== 'function' || typeof socket.getUSyncDevices !== 'function') {
      throw new Error('sender-key fanout socket is not bound');
    }

    await pendingInvalidations;
    const metadata = await socket.groupMetadata(chatId);
    const participants = Array.from(new Set((metadata?.participants || [])
      .map((participant) => participant?.id)
      .filter(Boolean)));
    trackMembership(chatId, participants);

    const previous = groupState.get(chatId) || { lastRefreshAt: 0, sendsSinceRefresh: 0, handledDeviceGeneration: 0 };
    const ttlDue = previous.lastRefreshAt === 0 || now() - previous.lastRefreshAt >= refreshTtlMs;
    const countDue = refreshEverySends > 0 && previous.sendsSinceRefresh >= refreshEverySends;
    const notificationDue = previous.handledDeviceGeneration < deviceGeneration;
    const refreshReason = notificationDue
      ? 'device-list-notification'
      : previous.lastRefreshAt === 0
      ? 'cold-start'
      : ttlDue
        ? 'ttl'
        : countDue
          ? 'send-count'
          : null;

    const devices = refreshReason
      ? await forceRefresh(chatId, participants, refreshReason)
      : await socket.getUSyncDevices(participants, true, false);
    const senderKeyResult = await authKeys.get('sender-key-memory', [chatId]);
    const senderKeyMap = senderKeyResult?.[chatId] || {};
    // Baileys 6.7.x returns { user, device }; rc13 additionally returns
    // server/jid. Match Baileys' own 6.x relay logic by deriving the server
    // from the group's addressing mode when it is absent.
    const defaultDeviceServer = metadata?.addressingMode === 'lid' ? 'lid' : 's.whatsapp.net';
    const recipientDevices = devices.map((device) => deviceJid(device, defaultDeviceServer)).filter(Boolean).sort();
    const senderKeyRecipients = devices
      .filter((device) => {
        const jid = deviceJid(device, defaultDeviceServer);
        return isSenderKeyEligible(device, defaultDeviceServer) && !senderKeyMap[jid];
      })
      .map((device) => deviceJid(device, defaultDeviceServer))
      .sort();
    const cachedKeyDevices = recipientDevices.filter((jid) => !!senderKeyMap[jid]);
    const nextState = refreshReason
      ? { lastRefreshAt: now(), sendsSinceRefresh: 1, handledDeviceGeneration: deviceGeneration }
      : { ...previous, sendsSinceRefresh: previous.sendsSinceRefresh + 1 };
    groupState.set(chatId, nextState);

    const context = {
      sequence: ++sequence,
      action,
      chatId,
      participants: participants.sort(),
      recipientDevices,
      senderKeyRecipients,
      cachedKeyDevices,
      refreshReason,
    };
    emitEvent({ phase: 'prepared', ...context });
    return context;
  }

  function completeGroupSend(context, { result = null, error = null } = {}) {
    if (!context) return;
    emitEvent({
      phase: 'completed',
      ...context,
      status: error ? 'error' : 'success',
      messageId: result?.key?.id || null,
      error: error ? (error?.message || String(error)) : null,
    });
  }

  return Object.freeze({
    userDevicesCache,
    bindSocket,
    noteDeviceListChange,
    prepareGroupSend,
    completeGroupSend,
    settle: () => pendingInvalidations,
  });
}
