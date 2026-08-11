import { strict as assert } from 'node:assert';

import {
  createMessageConsumerQueues,
  normalizeConsumerId,
  parseConsumerRoutes,
  selectConsumerForEvent,
} from './message_consumers.js';

assert.equal(normalizeConsumerId(), 'default');
assert.equal(normalizeConsumerId('Codex_WA'), 'codex_wa');
assert.equal(normalizeConsumerId('../bad'), null);

const routes = parseConsumerRoutes(JSON.stringify([
  { consumer: 'codex', prefix: '/codex', chat_ids: ['owner@lid'] },
  { consumer: 'opencode', match_all: true, chat_ids: ['owner@lid'] },
]));
assert.equal(routes.length, 2);
assert.equal(selectConsumerForEvent({ chatId: 'owner@lid', body: '/codex inspect status' }, routes), 'codex');
assert.equal(selectConsumerForEvent({ chatId: 'owner@lid', body: '/CODEX\ninspect status' }, routes), 'codex');
assert.equal(selectConsumerForEvent({ chatId: 'other@lid', body: '/codex inspect status' }, routes), 'default');
assert.equal(selectConsumerForEvent({ chatId: 'owner@lid', body: '/codexical' }, routes), 'opencode');
assert.equal(selectConsumerForEvent({ chatId: 'owner@lid', isGroup: true, body: '/codex inspect status' }, routes), 'default');
assert.equal(selectConsumerForEvent({ chatId: 'owner@lid', body: 'plain prompt' }, routes), 'opencode');

const reverseRoutes = parseConsumerRoutes(JSON.stringify([
  { consumer: 'opencode', match_all: true, chat_ids: ['owner@lid'] },
  { consumer: 'codex', prefix: '/codex', chat_ids: ['owner@lid'] },
]));
assert.equal(selectConsumerForEvent({ chatId: 'owner@lid', body: '/codex task' }, reverseRoutes), 'codex');
assert.throws(
  () => parseConsumerRoutes([{ consumer: 'default', prefix: '/bad', chat_ids: ['owner@lid'] }]),
  /non-default/,
);
assert.throws(
  () => parseConsumerRoutes([
    { consumer: 'one', match_all: true, chat_ids: ['owner@lid'] },
    { consumer: 'two', match_all: true, chat_ids: ['owner@lid'] },
  ]),
  /Duplicate catch-all/,
);

const queues = createMessageConsumerQueues(2, ['codex']);
queues.enqueue({ id: 'h1' });
queues.enqueue({ id: 'c1' }, 'codex');
queues.enqueue({ id: 'c2' }, 'codex');
queues.enqueue({ id: 'c3' }, 'codex');
assert.deepEqual(queues.drain(), [{ id: 'h1' }]);
assert.deepEqual(queues.drain('codex'), [{ id: 'c2' }, { id: 'c3' }]);
assert.equal(queues.drain('unconfigured'), null);

const leasedQueues = createMessageConsumerQueues(2, ['opencode']);
leasedQueues.enqueue({ id: 'o1' }, 'opencode');
const firstLease = leasedQueues.lease('opencode', 1000, 1, 100);
assert.equal(firstLease.length, 1);
assert.equal(firstLease[0].event.id, 'o1');
assert.deepEqual(leasedQueues.lease('opencode', 1000, 1, 500), []);
const redelivery = leasedQueues.lease('opencode', 1000, 1, 1101);
assert.equal(redelivery[0].event.id, 'o1');
assert.equal(leasedQueues.ack('opencode', redelivery[0].deliveryId), true);
assert.deepEqual(leasedQueues.lease('opencode', 1000, 1, 2200), []);
console.log('✓ named consumer routing keeps default and Codex queues isolated');
