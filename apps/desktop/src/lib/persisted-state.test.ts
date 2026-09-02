import assert from 'node:assert/strict'

import { describe, test } from 'vitest'

import { definePersistedState } from './persisted-state'

describe('persisted-state (§14)', () => {
  test('global scope key has no route suffix', () => {
    const s = definePersistedState({ name:'theme', scope:'global', version:1 })
    assert.equal(s.storageKey({}), 'hermes:theme:v1')
  })
  test('route scope key incorporates routeKey', () => {
    const s = definePersistedState({ name:'pinned-sessions', scope:'route', version:3 })
    assert.equal(s.storageKey({ routeKey:'conn:homelab::default' }), 'hermes:pinned-sessions:v3:route:conn:homelab::default')
    assert.throws(()=> s.storageKey({}))
  })
  test('connection scope key incorporates connectionId', () => {
    const s = definePersistedState({ name:'creds', scope:'connection', version:1 })
    assert.equal(s.storageKey({ connectionId:'homelab' }), 'hermes:creds:v1:conn:homelab')
  })
})
