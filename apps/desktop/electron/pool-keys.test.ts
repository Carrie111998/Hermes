import assert from 'node:assert/strict'

import { test } from 'vitest'

import { makeBackendTarget } from './backend-target'
import {
  configuredPoolKey,
  isForcedLocalTarget,
  poolKeyForTarget,
  poolKeysForProfile,
  poolRouteForTarget
} from './pool-keys'

// ---------------------------------------------------------------------------
// poolKeyForTarget — the canonical pool key derived from a BackendTarget.
// Equivalent windows share the key; configured vs forced-local routes for the
// same profile are distinct.
// ---------------------------------------------------------------------------

test('poolKeyForTarget maps primary to the primary sentinel', () => {
  assert.equal(poolKeyForTarget(makeBackendTarget({ kind: 'primary' })), 'primary')
})

test('poolKeyForTarget maps a configured profile to a configured-route key', () => {
  assert.equal(poolKeyForTarget(makeBackendTarget({ kind: 'configured-profile', profile: 'worker' })), 'configured-profile:worker')
})

test('poolKeyForTarget maps a forced-local profile to a forced-local key', () => {
  assert.equal(poolKeyForTarget(makeBackendTarget({ kind: 'forced-local-profile', profile: 'worker' })), 'forced-local-profile:worker')
})

test('poolKeyForTarget keeps configured and forced-local distinct for the same profile', () => {
  const configured = makeBackendTarget({ kind: 'configured-profile', profile: 'worker' })
  const forced = makeBackendTarget({ kind: 'forced-local-profile', profile: 'worker' })

  assert.notEqual(poolKeyForTarget(configured), poolKeyForTarget(forced))
})

test('configuredPoolKey never falls back to the legacy raw profile key', () => {
  assert.equal(configuredPoolKey('worker'), 'configured-profile:worker')
  assert.notEqual(configuredPoolKey('worker'), 'worker')
})

test('poolKeysForProfile returns every route entry for profile teardown', () => {
  const entries = [
    ['configured-profile:worker', { profile: 'worker' }],
    ['forced-local-profile:worker', { profile: 'worker' }],
    ['configured-profile:coder', { profile: 'coder' }]
  ] as const

  assert.deepEqual(poolKeysForProfile(entries, 'worker'), [
    'configured-profile:worker',
    'forced-local-profile:worker'
  ])
})

test('poolKeyForTarget never incorporates a window id', () => {
  // The key is derived solely from the target identity.
  const target = makeBackendTarget({ kind: 'forced-local-profile', profile: 'coder' })

  assert.equal(poolKeyForTarget(target), 'forced-local-profile:coder')
})

// ---------------------------------------------------------------------------
// poolRouteForTarget — the routing decision a pool operator needs: which
// backend branch to take, what real profile to keep on the entry, and the
// canonical pool key. Forced-local routes must bypass remote resolution.
// ---------------------------------------------------------------------------

test('poolRouteForTarget returns the primary route for primary', () => {
  const route = poolRouteForTarget(makeBackendTarget({ kind: 'primary' }))

  assert.deepEqual(route, {
    route: 'primary',
    profile: null,
    key: 'primary'
  })
})

test('poolRouteForTarget returns the configured route for a configured profile', () => {
  const route = poolRouteForTarget(makeBackendTarget({ kind: 'configured-profile', profile: 'worker' }))

  assert.deepEqual(route, {
    route: 'configured',
    profile: 'worker',
    key: 'configured-profile:worker'
  })
})

test('poolRouteForTarget returns the forced-local route for a forced-local profile', () => {
  const route = poolRouteForTarget(makeBackendTarget({ kind: 'forced-local-profile', profile: 'worker' }))

  assert.deepEqual(route, {
    route: 'forced-local',
    profile: 'worker',
    key: 'forced-local-profile:worker'
  })
})

test('poolRouteForTarget keeps configured and forced-local keys distinct for the same profile', () => {
  const configured = poolRouteForTarget(makeBackendTarget({ kind: 'configured-profile', profile: 'worker' }))
  const forced = poolRouteForTarget(makeBackendTarget({ kind: 'forced-local-profile', profile: 'worker' }))

  assert.notEqual(configured.key, forced.key)
  // Both carry the same real profile name.
  assert.equal(configured.profile, forced.profile)
})

test('poolRouteForTarget maps configured-connection:local onto the forced-local default pool', () => {
  const route = poolRouteForTarget(makeBackendTarget({ kind: 'configured-connection', connection: 'local' }))

  assert.deepEqual(route, {
    route: 'forced-local',
    profile: 'default',
    key: 'forced-local-profile:default'
  })
})

test('poolRouteForTarget keeps a remote registry connection on its own key', () => {
  const route = poolRouteForTarget(makeBackendTarget({ kind: 'configured-connection', connection: 'atrium-agents' }))

  assert.deepEqual(route, {
    route: 'connection',
    profile: null,
    key: 'configured-connection:atrium-agents'
  })
})

// ---------------------------------------------------------------------------
// isForcedLocalTarget — whether the pool operator must bypass remote
// resolution for this target and spawn a local profile process.
// ---------------------------------------------------------------------------

test('isForcedLocalTarget is true for a forced-local target', () => {
  assert.equal(isForcedLocalTarget(makeBackendTarget({ kind: 'forced-local-profile', profile: 'worker' })), true)
})

test('isForcedLocalTarget is false for a primary target', () => {
  assert.equal(isForcedLocalTarget(makeBackendTarget({ kind: 'primary' })), false)
})

test('isForcedLocalTarget is false for a configured-profile target', () => {
  // A configured-profile target follows the existing ensureBackend(profile)
  // path, which may resolve remotely. Only forced-local bypasses that.
  assert.equal(isForcedLocalTarget(makeBackendTarget({ kind: 'configured-profile', profile: 'worker' })), false)
})