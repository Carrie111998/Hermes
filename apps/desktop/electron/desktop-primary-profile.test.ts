import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  isValidProfileName,
  resolveDesktopPrimaryProfile
} from './desktop-primary-profile'

test('returns the desktop-stored profile when set', () => {
  assert.equal(resolveDesktopPrimaryProfile('security-analyst', 'software-engineer'), 'security-analyst')
})

test('returns the desktop-stored profile even when it equals the CLI sticky', () => {
  // The desktop pref is the user telling us "always use this", so a stored
  // value of 'default' still pins explicitly rather than falling through.
  assert.equal(resolveDesktopPrimaryProfile('default', 'software-engineer'), 'default')
})

test('falls back to the CLI sticky profile when the desktop pref is unset', () => {
  // The bug we're fixing: desktop used to fall straight to 'default' here.
  assert.equal(resolveDesktopPrimaryProfile(null, 'software-engineer'), 'software-engineer')
})

test('falls back to the CLI sticky profile when the desktop pref is empty string', () => {
  assert.equal(resolveDesktopPrimaryProfile('', 'security-analyst'), 'security-analyst')
})

test('falls back to "default" when neither preference is set', () => {
  assert.equal(resolveDesktopPrimaryProfile(null, null), 'default')
  assert.equal(resolveDesktopPrimaryProfile('', ''), 'default')
  assert.equal(resolveDesktopPrimaryProfile(null, ''), 'default')
})

test('trims whitespace around the CLI sticky file value', () => {
  // The CLI writes "<name>\n" — defensive trim keeps a stray newline from
  // slipping through to the backend as "--profile software-engineer\n".
  assert.equal(resolveDesktopPrimaryProfile(null, '  software-engineer  '), 'software-engineer')
})

test('accepts the canonical profile names', () => {
  assert.equal(isValidProfileName('default'), true)
  assert.equal(isValidProfileName('software-engineer'), true)
  assert.equal(isValidProfileName('security-analyst'), true)
  assert.equal(isValidProfileName('work-vps-1'), true)
})

test('rejects invalid profile names', () => {
  assert.equal(isValidProfileName(''), false)
  assert.equal(isValidProfileName('   '), false)
  assert.equal(isValidProfileName('-leading-dash'), false)
  assert.equal(isValidProfileName('Has Spaces'), false)
  assert.equal(isValidProfileName('A'.repeat(65)), false)
})
