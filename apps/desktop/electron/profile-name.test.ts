import assert from 'node:assert/strict'

import { test } from 'vitest'

import { normalizeDesktopProfile } from './profile-name'

test('normalizeDesktopProfile accepts canonical desktop profile names', () => {
  assert.equal(normalizeDesktopProfile(' life_2 '), 'life_2')
  assert.equal(normalizeDesktopProfile('default'), 'default')
})

test('normalizeDesktopProfile rejects malformed and absent values', () => {
  assert.equal(normalizeDesktopProfile('../life'), null)
  assert.equal(normalizeDesktopProfile('bad profile'), null)
  assert.equal(normalizeDesktopProfile(''), null)
  assert.equal(normalizeDesktopProfile(undefined), null)
})
