import assert from 'node:assert/strict'

import { test } from 'vitest'

import { isMultiplexProfileRead, resolveApiRouteProfile, shouldIdleStopPrimary } from './primary-idle'

test('idle-stop when UI is on a named profile, primary is a local child, keep excludes primary', () => {
  assert.equal(
    shouldIdleStopPrimary({
      activeProfile: 'repro91050',
      primaryKey: 'default',
      keepProfiles: ['repro91050'],
      primaryMode: 'local-child'
    }),
    true
  )
})

test('do not idle-stop when the UI is still on the primary', () => {
  assert.equal(
    shouldIdleStopPrimary({
      activeProfile: 'default',
      primaryKey: 'default',
      keepProfiles: [],
      primaryMode: 'local-child'
    }),
    false
  )
})

test('do not idle-stop when a working/attention session belongs to primary', () => {
  assert.equal(
    shouldIdleStopPrimary({
      activeProfile: 'repro91050',
      primaryKey: 'default',
      keepProfiles: ['default', 'repro91050'],
      primaryMode: 'local-child'
    }),
    false
  )
})

test('treat empty activeProfile as default (legacy)', () => {
  assert.equal(
    shouldIdleStopPrimary({
      activeProfile: '',
      primaryKey: 'default',
      keepProfiles: [],
      primaryMode: 'local-child'
    }),
    false
  )
})

test('do not idle-stop a remote primary or a primary that is already down', () => {
  for (const primaryMode of ['remote', 'down']) {
    assert.equal(
      shouldIdleStopPrimary({
        activeProfile: 'repro91050',
        primaryKey: 'default',
        keepProfiles: [],
        primaryMode
      }),
      false
    )
  }
})

test('isMultiplexProfileRead covers only cross-profile GET list/active', () => {
  assert.equal(isMultiplexProfileRead('GET', '/api/profiles/sessions'), true)
  assert.equal(isMultiplexProfileRead('GET', '/api/profiles'), true)
  assert.equal(isMultiplexProfileRead('GET', '/api/profiles/active'), true)
  assert.equal(isMultiplexProfileRead('DELETE', '/api/profiles/sessions'), false)
  assert.equal(isMultiplexProfileRead('GET', '/api/status'), false)
  assert.equal(isMultiplexProfileRead('GET', '/api/sessions'), false)
})

test('resolveApiRouteProfile keeps stamped profile (named or default)', () => {
  assert.equal(
    resolveApiRouteProfile({
      requestProfile: 'repro91050',
      tornDownProfile: null,
      primaryRunning: false,
      livePoolKeys: ['repro91050'],
      lastActiveProfile: 'repro91050',
      method: 'GET',
      pathname: '/api/profiles/sessions'
    }),
    'repro91050'
  )
  assert.equal(
    resolveApiRouteProfile({
      requestProfile: 'default',
      tornDownProfile: null,
      primaryRunning: true,
      livePoolKeys: [],
      lastActiveProfile: 'default',
      method: 'GET',
      pathname: '/api/profiles/sessions'
    }),
    'default'
  )
})

test('unscoped multiplex GET falls back to a live pool key when primary is down', () => {
  assert.equal(
    resolveApiRouteProfile({
      requestProfile: null,
      tornDownProfile: null,
      primaryRunning: false,
      livePoolKeys: ['repro91050'],
      lastActiveProfile: 'repro91050',
      method: 'GET',
      pathname: '/api/profiles/sessions'
    }),
    'repro91050'
  )
})

test('unscoped non-multiplex still routes to primary (null) even if primary is down', () => {
  assert.equal(
    resolveApiRouteProfile({
      requestProfile: null,
      tornDownProfile: null,
      primaryRunning: false,
      livePoolKeys: ['repro91050'],
      lastActiveProfile: 'repro91050',
      method: 'GET',
      pathname: '/api/status'
    }),
    null
  )
})

test('profile-delete tornDownProfile still forces primary (null) — do not change that contract', () => {
  assert.equal(
    resolveApiRouteProfile({
      requestProfile: 'doomed',
      tornDownProfile: 'doomed',
      primaryRunning: true,
      livePoolKeys: [],
      lastActiveProfile: 'default',
      method: 'DELETE',
      pathname: '/api/profiles/doomed'
    }),
    null
  )
})
