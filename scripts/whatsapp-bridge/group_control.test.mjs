import { strict as assert } from 'node:assert';
import { mkdtempSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

import {
  GroupOperationStore,
  authorizeGroupControl,
  executeGroupCreate,
  parseParticipantAllowlist,
  validateGroupCreatePayload,
} from './group_control.js';

const allowed = parseParticipantAllowlist(
  '15550001111@s.whatsapp.net,998877@lid',
);
const request = {
  subject: 'Codex Blockers',
  confirmedSubject: 'Codex Blockers',
  operationId: 'codex-blockers-001',
  participants: ['15550001111:2@c.us'],
  confirmedParticipants: ['15550001111:2@c.us'],
};

const token = '0123456789abcdef0123456789abcdef';
assert.equal(authorizeGroupControl(`Bearer ${token}`, token), true);
assert.equal(authorizeGroupControl('Bearer short', 'short'), false);
assert.equal(authorizeGroupControl('', token), false);
assert.deepEqual([...allowed], ['15550001111@s.whatsapp.net', '998877@lid']);
assert.deepEqual(
  validateGroupCreatePayload(request, allowed).participants,
  ['15550001111@s.whatsapp.net'],
);
assert.equal(
  validateGroupCreatePayload({ ...request, confirmedSubject: 'different' }, allowed),
  null,
);
assert.equal(
  validateGroupCreatePayload({ ...request, confirmedParticipants: ['998877@lid'] }, allowed),
  null,
);
assert.equal(
  validateGroupCreatePayload(
    { ...request, participants: ['17770000000@s.whatsapp.net'] },
    allowed,
  ),
  null,
);
console.log('  ✓ group control authentication and exact allowlists fail closed');

const root = mkdtempSync(path.join(tmpdir(), 'hermes-group-control-'));
try {
  let now = 1_000_000;
  const statePath = path.join(root, 'private', 'operations.json');
  const store = new GroupOperationStore(statePath, { now: () => now });
  let creates = 0;
  const created = await executeGroupCreate({
    body: request,
    allowedParticipants: allowed,
    store,
    now: () => now,
    listGroups: async () => ({}),
    createGroup: async (subject, participants) => {
      creates += 1;
      assert.equal(subject, 'Codex Blockers');
      assert.deepEqual(participants, ['15550001111@s.whatsapp.net']);
      return { id: '120363001234567890@g.us' };
    },
  });
  assert.equal(created.httpStatus, 201);
  assert.equal(created.body.groupId, '120363001234567890@g.us');
  assert.equal(statSync(path.dirname(statePath)).mode & 0o777, 0o700);
  assert.equal(statSync(statePath).mode & 0o777, 0o600);

  const replay = await executeGroupCreate({
    body: request,
    allowedParticipants: allowed,
    store,
    now: () => now,
    listGroups: async () => { throw new Error('must not re-check'); },
    createGroup: async () => { throw new Error('must not recreate'); },
  });
  assert.equal(replay.httpStatus, 200);
  assert.equal(replay.body.groupId, created.body.groupId);
  assert.equal(creates, 1);

  const changedReplay = await executeGroupCreate({
    body: { ...request, subject: 'Changed', confirmedSubject: 'Changed' },
    allowedParticipants: allowed,
    store,
    now: () => now,
    listGroups: async () => ({}),
    createGroup: async () => ({ id: 'unused@g.us' }),
  });
  assert.equal(changedReplay.httpStatus, 409);
  assert.equal(changedReplay.body.status, 'conflict');
  console.log('  ✓ created operations persist privately and replay idempotently');

  now += 30_001;
  const duplicateSubject = await executeGroupCreate({
    body: { ...request, operationId: 'codex-blockers-002' },
    allowedParticipants: allowed,
    store,
    now: () => now,
    listGroups: async () => ({ existing: { subject: 'Codex Blockers' } }),
    createGroup: async () => ({ id: 'unused@g.us' }),
  });
  assert.equal(duplicateSubject.httpStatus, 409);
  assert.equal(duplicateSubject.body.status, 'subject-exists');

  const uncertain = await executeGroupCreate({
    body: {
      subject: 'Operations',
      confirmedSubject: 'Operations',
      operationId: 'operations-group-001',
      participants: ['998877@lid'],
      confirmedParticipants: ['998877@lid'],
    },
    allowedParticipants: allowed,
    store,
    now: () => now,
    minimumIntervalMs: 0,
    listGroups: async () => ({}),
    createGroup: async () => { throw new Error('network result unknown'); },
  });
  assert.equal(uncertain.httpStatus, 502);
  assert.equal(uncertain.body.status, 'uncertain');
  const uncertainReplay = await executeGroupCreate({
    body: {
      subject: 'Operations',
      confirmedSubject: 'Operations',
      operationId: 'operations-group-001',
      participants: ['998877@lid'],
      confirmedParticipants: ['998877@lid'],
    },
    allowedParticipants: allowed,
    store,
    now: () => now,
    minimumIntervalMs: 0,
    listGroups: async () => ({}),
    createGroup: async () => ({ id: 'must-not-run@g.us' }),
  });
  assert.equal(uncertainReplay.httpStatus, 409);
  assert.equal(uncertainReplay.body.status, 'uncertain');
  console.log('  ✓ duplicate subjects and uncertain operations never create twice');
} finally {
  rmSync(root, { recursive: true, force: true });
}
