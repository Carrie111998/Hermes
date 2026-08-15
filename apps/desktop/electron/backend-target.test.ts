import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  type BackendTarget,
  canonicalTargetKey,
  makeBackendTarget
} from './backend-target'
import { isValidProfileName } from './profile-name'

// ---------------------------------------------------------------------------
// isValidProfileName — mirrors hermes_cli.profiles.validate_profile_name()
// ---------------------------------------------------------------------------

test('isValidProfileName accepts the default alias', () => {
  assert.equal(isValidProfileName('default'), true)
})

test('isValidProfileName accepts a normal profile id', () => {
  assert.equal(isValidProfileName('worker'), true)
  assert.equal(isValidProfileName('my-profile_1'), true)
})

test('isValidProfileName rejects names that violate the id regex', () => {
  assert.equal(isValidProfileName(''), false)
  assert.equal(isValidProfileName('UPPER'), false)
  assert.equal(isValidProfileName('has space'), false)
  assert.equal(isValidProfileName('-leading-dash'), false)
  assert.equal(isValidProfileName('dot.name'), false)
})

test('isValidProfileName rejects reserved names that collide on disk', () => {
  assert.equal(isValidProfileName('hermes'), false)
  assert.equal(isValidProfileName('test'), false)
  assert.equal(isValidProfileName('tmp'), false)
  assert.equal(isValidProfileName('root'), false)
  assert.equal(isValidProfileName('sudo'), false)
})

// ---------------------------------------------------------------------------
// makeBackendTarget — closed variants, validated at construction
// ---------------------------------------------------------------------------

test('makeBackendTarget builds a primary target', () => {
  assert.deepEqual(makeBackendTarget({ kind: 'primary' }), { kind: 'primary' })
})

test('makeBackendTarget builds a configured-profile target', () => {
  assert.deepEqual(makeBackendTarget({ kind: 'configured-profile', profile: 'worker' }), {
    kind: 'configured-profile',
    profile: 'worker'
  })
})

test('makeBackendTarget builds a forced-local-profile target', () => {
  assert.deepEqual(makeBackendTarget({ kind: 'forced-local-profile', profile: 'worker' }), {
    kind: 'forced-local-profile',
    profile: 'worker'
  })
})

test('makeBackendTarget rejects an invalid profile name for configured-profile', () => {
  assert.throws(() => makeBackendTarget({ kind: 'configured-profile', profile: 'Not Valid!' }), /Invalid profile name/)
})

test('makeBackendTarget rejects a reserved profile name for forced-local-profile', () => {
  assert.throws(() => makeBackendTarget({ kind: 'forced-local-profile', profile: 'hermes' }), /Invalid profile name/)
})

test('makeBackendTarget rejects a blank profile name', () => {
  assert.throws(() => makeBackendTarget({ kind: 'configured-profile', profile: '  ' }), /Invalid profile name/)
})

test('makeBackendTarget rejects an unknown kind', () => {
  assert.throws(
    () => makeBackendTarget({ kind: 'remote-url', url: 'wss://evil.example' } as unknown as BackendTarget),
    /Unknown target kind/
  )
})

// ---------------------------------------------------------------------------
// canonicalTargetKey — equivalent targets share pool identity
// ---------------------------------------------------------------------------

test('canonicalTargetKey maps primary to a fixed primary sentinel', () => {
  assert.equal(canonicalTargetKey({ kind: 'primary' }), 'primary')
})

test('canonicalTargetKey maps a configured profile to a configured-route key', () => {
  assert.equal(
    canonicalTargetKey({ kind: 'configured-profile', profile: 'worker' }),
    'configured-profile:worker'
  )
})

test('canonicalTargetKey maps a forced-local profile to a local-only key', () => {
  assert.equal(
    canonicalTargetKey({ kind: 'forced-local-profile', profile: 'worker' }),
    'forced-local-profile:worker'
  )
})

test('canonicalTargetKey keeps configured and forced-local routes distinct for the same profile', () => {
  const configured = makeBackendTarget({ kind: 'configured-profile', profile: 'worker' })
  const forced = makeBackendTarget({ kind: 'forced-local-profile', profile: 'worker' })

  assert.notEqual(canonicalTargetKey(configured), canonicalTargetKey(forced))
})

test('canonicalTargetKey distinguishes different profiles', () => {
  assert.notEqual(
    canonicalTargetKey({ kind: 'configured-profile', profile: 'worker' }),
    canonicalTargetKey({ kind: 'configured-profile', profile: 'coder' })
  )
})

test('canonicalTargetKey distinguishes primary from any named profile', () => {
  assert.notEqual(
    canonicalTargetKey({ kind: 'primary' }),
    canonicalTargetKey({ kind: 'configured-profile', profile: 'default' })
  )
})

test('canonicalTargetKey never incorporates a window id or scope', () => {
  // The key is derived only from the target; there is no surface for window
  // ids or scopes to enter the key, so the same target always yields the same
  // key regardless of any caller-provided window context.
  const target = makeBackendTarget({ kind: 'forced-local-profile', profile: 'worker' })

  assert.equal(canonicalTargetKey(target), 'forced-local-profile:worker')
  assert.equal(canonicalTargetKey(target), 'forced-local-profile:worker')
})