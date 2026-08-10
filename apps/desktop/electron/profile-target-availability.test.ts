import assert from 'node:assert/strict'
import path from 'node:path'

import { test } from 'vitest'

import { isProfileTargetAvailable } from './profile-target-availability'

test('default is always available without consulting the profile directory', () => {
  let checked = false

  assert.equal(isProfileTargetAvailable('default', '/home/alice', () => {
    checked = true

    return false
  }), true)
  assert.equal(checked, false)
})

test('named targets require an existing HOME-anchored profile directory', () => {
  const expected = path.join('/home/alice', '.hermes', 'profiles', 'worker')
  let checkedPath = ''

  assert.equal(isProfileTargetAvailable('worker', '/home/alice', candidate => {
    checkedPath = candidate

    return candidate === expected
  }), true)
  assert.equal(checkedPath, expected)
  assert.equal(isProfileTargetAvailable('missing', '/home/alice', () => false), false)
})

test('invalid and reserved profile names are unavailable without filesystem access', () => {
  let checks = 0

  const exists = () => {
    checks += 1

    return true
  }

  assert.equal(isProfileTargetAvailable('../escape', '/home/alice', exists), false)
  assert.equal(isProfileTargetAvailable('hermes', '/home/alice', exists), false)

  assert.equal(checks, 0)
})
