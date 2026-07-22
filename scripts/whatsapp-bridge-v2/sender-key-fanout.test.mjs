import assert from 'node:assert/strict';
import test from 'node:test';

import { createSenderKeyFanoutManager } from './sender-key-fanout.js';

function memoryKeys(initial = {}) {
  const values = new Map(Object.entries(initial));
  return {
    values,
    async get(type, ids) {
      return Object.fromEntries(ids.map((id) => [id, values.get(`${type}:${id}`) ?? null]));
    },
    async set(data) {
      for (const [type, records] of Object.entries(data)) {
        for (const [id, value] of Object.entries(records)) {
          const key = `${type}:${id}`;
          if (value === null) values.delete(key);
          else values.set(key, value);
        }
      }
    },
  };
}

function devices(...ids) {
  return ids.map((device) => ({
    user: '111',
    server: 'lid',
    device,
    jid: device === 0 ? '111@lid' : `111:${device}@lid`,
  }));
}

test('device appearing after a warm cache receives sender key on the next send', async () => {
  const chatId = '120363000000000000@g.us';
  const keys = memoryKeys({ [`sender-key-memory:${chatId}`]: { '111@lid': true } });
  const events = [];
  let currentDevices = devices(0);
  const manager = createSenderKeyFanoutManager({
    authKeys: keys,
    refreshTtlMs: 60_000,
    refreshEverySends: 100,
    emit: (event) => events.push(event),
  });
  const socket = {
    async groupMetadata() {
      return { participants: [{ id: '111@lid' }] };
    },
    async getUSyncDevices(participants, useCache) {
      if (useCache) return manager.userDevicesCache.get('111') || currentDevices;
      manager.userDevicesCache.set('111', currentDevices);
      return currentDevices;
    },
  };
  manager.bindSocket(socket);

  const warm = await manager.prepareGroupSend(chatId);
  assert.deepEqual(warm.senderKeyRecipients, ['111@lid']);
  // The desktop keeps the same device JID after losing its local sender key.
  // This is the rc13 failure shape: the persisted boolean still says keyed.
  keys.values.set(`sender-key-memory:${chatId}`, { '111@lid': true, '111:8@lid': true });

  currentDevices = devices(0, 8);
  manager.noteDeviceListChange({ from: '111@lid', tag: 'add' });
  manager.userDevicesCache.set('111', currentDevices); // Baileys handler applies the notification
  await manager.settle();

  const afterCompanionAppears = await manager.prepareGroupSend(chatId);
  assert.equal(afterCompanionAppears.refreshReason, 'device-list-notification');
  assert.deepEqual(afterCompanionAppears.recipientDevices, ['111:8@lid', '111@lid']);
  assert.deepEqual(afterCompanionAppears.senderKeyRecipients, ['111:8@lid', '111@lid']);
  assert.ok(events.some((event) => event.phase === 'invalidation' && event.reason === 'device-list-notification'));
});

test('ttl fallback bypasses device cache and redistributes sender keys', async () => {
  const chatId = '120363000000000001@g.us';
  const keys = memoryKeys();
  const calls = [];
  let clock = 1_000;
  let currentDevices = devices(0);
  const manager = createSenderKeyFanoutManager({
    authKeys: keys,
    now: () => clock,
    refreshTtlMs: 1_000,
    refreshEverySends: 100,
    emit: () => {},
  });
  manager.bindSocket({
    async groupMetadata() {
      return { participants: [{ id: '111@lid' }] };
    },
    async getUSyncDevices(participants, useCache) {
      calls.push(useCache);
      if (useCache) return manager.userDevicesCache.get('111') || currentDevices;
      manager.userDevicesCache.set('111', currentDevices);
      return currentDevices;
    },
  });

  await manager.prepareGroupSend(chatId);
  keys.values.set(`sender-key-memory:${chatId}`, { '111@lid': true, '111:8@lid': true });
  currentDevices = devices(0, 8);
  clock += 1_001;
  const refreshed = await manager.prepareGroupSend(chatId);

  assert.deepEqual(calls, [false, false]);
  assert.equal(refreshed.refreshReason, 'ttl');
  assert.deepEqual(refreshed.senderKeyRecipients, ['111:8@lid', '111@lid']);
});
