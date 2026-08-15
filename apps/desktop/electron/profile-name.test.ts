import assert from 'node:assert/strict'

import { test } from 'vitest'

import { isValidProfileName, normalizeDesktopProfile, PROFILE_NAME_RE } from './profile-name'

// ---------------------------------------------------------------------------
// PROFILE_NAME_RE — the raw id regex, exported so main.ts does not keep a
// private duplicate. This is the raw pattern only (no default exemption, no
// reserved-name rejection); callers compose those checks themselves exactly
// as the Python validator does.
// ---------------------------------------------------------------------------

test('PROFILE_NAME_RE is a RegExp that matches the profile id pattern', () => {
  // The exported regex must behave as the raw pattern: /^[a-z0-9][a-z0-9_-]{0,63}$/.
  // We assert behavior (what it accepts/rejects), not the literal source.
  assert.equal(PROFILE_NAME_RE.test('default'), true)
  assert.equal(PROFILE_NAME_RE.test('worker'), true)
  assert.equal(PROFILE_NAME_RE.test('my-profile_1'), true)
  assert.equal(PROFILE_NAME_RE.test('a'), true)
})

test('PROFILE_NAME_RE rejects uppercase, spaces, leading dashes, dots', () => {
  assert.equal(PROFILE_NAME_RE.test(''), false)
  assert.equal(PROFILE_NAME_RE.test('UPPER'), false)
  assert.equal(PROFILE_NAME_RE.test('has space'), false)
  assert.equal(PROFILE_NAME_RE.test('-leading-dash'), false)
  assert.equal(PROFILE_NAME_RE.test('dot.name'), false)
})

test('isValidProfileName validates the identifier as given without trimming', () => {
  assert.equal(isValidProfileName(' worker '), false)
  assert.equal(isValidProfileName('worker '), false)
  assert.equal(isValidProfileName(' worker'), false)
})

test('PROFILE_NAME_RE accepts reserved names at the raw pattern level', () => {
  // The raw regex does NOT reject reserved names — that is the job of
  // isValidProfileName. main.ts callers that want raw-pattern matching
  // (e.g. sanitizeConnectionProfiles) intentionally accept reserved names
  // as connection-scope keys, so the exported regex must not add that layer.
  assert.equal(PROFILE_NAME_RE.test('hermes'), true)
  assert.equal(PROFILE_NAME_RE.test('test'), true)
  assert.equal(PROFILE_NAME_RE.test('tmp'), true)
})

test('PROFILE_NAME_RE is the raw pattern isValidProfileName uses internally', () => {
  // For every non-reserved, non-empty name that matches the raw regex,
  // isValidProfileName must also return true — proving they share one
  // pattern, not two drift-prone copies. (Reserved names are rejected by
  // isValidProfileName's extra layer, so they are excluded from this
  // agreement check by construction.)
  for (const name of ['default', 'worker', 'my-profile_1', 'a', 'UPPER', 'has space']) {
    if (PROFILE_NAME_RE.test(name) && name !== 'default') {
      assert.equal(isValidProfileName(name), true, `regex accepted but validator rejected "${name}"`)
    }
  }
})

test('normalizeDesktopProfile accepts and canonicalizes desktop profile names', () => {
  assert.equal(normalizeDesktopProfile(' life_2 '), 'life_2')
  assert.equal(normalizeDesktopProfile('Default'), 'default')
})

test('normalizeDesktopProfile rejects malformed, absent, and reserved values', () => {
  for (const profile of ['../life', 'bad profile', '', undefined, 'hermes', 'test', 'tmp', 'root', 'sudo']) {
    assert.equal(normalizeDesktopProfile(profile), null, String(profile))
  }
})
