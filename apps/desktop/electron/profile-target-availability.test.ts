import assert from 'node:assert/strict'
import path from 'node:path'

import { test } from 'vitest'

import {
  configuredProfileExistsOnSharedRemote,
  isProfileTargetAvailable
} from './profile-target-availability'

test('default is always available without consulting the profile directory', () => {
  let checked = false

  assert.equal(isProfileTargetAvailable('default', '/opt/hermes-data', () => {
    checked = true

    return false
  }), true)
  assert.equal(checked, false)
})

test('named targets resolve from the Desktop HERMES_HOME', () => {
  const expected = path.join('/opt/hermes-data', 'profiles', 'worker')
  let checkedPath = ''

  assert.equal(isProfileTargetAvailable('worker', '/opt/hermes-data', candidate => {
    checkedPath = candidate

    return candidate === expected
  }), true)
  assert.equal(checkedPath, expected)
  assert.equal(isProfileTargetAvailable('missing', '/opt/hermes-data', () => false), false)
})

test('invalid and reserved profile names are unavailable without filesystem access', () => {
  let checks = 0

  const exists = () => {
    checks += 1

    return true
  }

  assert.equal(isProfileTargetAvailable('../escape', '/opt/hermes-data', exists), false)
  assert.equal(isProfileTargetAvailable('hermes', '/opt/hermes-data', exists), false)

  assert.equal(checks, 0)
})

test('shared global remote treats valid remote-only names as openable', () => {
  assert.equal(configuredProfileExistsOnSharedRemote('alan', true), true)
  assert.equal(configuredProfileExistsOnSharedRemote('default', true), true)
  assert.equal(configuredProfileExistsOnSharedRemote('alan', false), false)
  assert.equal(configuredProfileExistsOnSharedRemote('hermes', true), false)
  assert.equal(configuredProfileExistsOnSharedRemote('../escape', true), false)
})
