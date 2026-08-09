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
  { consumer: 'default', prefix: '/ignored', chat_ids: ['owner@lid'] },
]));
assert.equal(routes.length, 1);
assert.equal(selectConsumerForEvent({ chatId: 'owner@lid', body: '/codex inspect status' }, routes), 'codex');
assert.equal(selectConsumerForEvent({ chatId: 'owner@lid', body: '/CODEX\ninspect status' }, routes), 'codex');
assert.equal(selectConsumerForEvent({ chatId: 'other@lid', body: '/codex inspect status' }, routes), 'default');
assert.equal(selectConsumerForEvent({ chatId: 'owner@lid', body: '/codexical' }, routes), 'default');

const queues = createMessageConsumerQueues(2);
queues.enqueue({ id: 'h1' });
queues.enqueue({ id: 'c1' }, 'codex');
queues.enqueue({ id: 'c2' }, 'codex');
queues.enqueue({ id: 'c3' }, 'codex');
assert.deepEqual(queues.drain(), [{ id: 'h1' }]);
assert.deepEqual(queues.drain('codex'), [{ id: 'c2' }, { id: 'c3' }]);
console.log('✓ named consumer routing keeps default and Codex queues isolated');
