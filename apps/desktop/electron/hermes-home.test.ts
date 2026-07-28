import assert from 'node:assert/strict'

import { test } from 'vitest'

import { resolveHermesHome } from './hermes-home'

const neverExists = () => false

test('resolveHermesHome normalizes an explicit home override', () => {
  const home = resolveHermesHome({
    directoryExists: neverExists,
    homeDirectory: '/Users/hermes',
    isWindows: false,
    env: { HERMES_HOME: '/data/profiles/coder' }
  })

  assert.equal(home, '/data')
})

test('resolveHermesHome uses the platform default without an override', () => {
  const home = resolveHermesHome({
    directoryExists: neverExists,
    homeDirectory: '/Users/hermes',
    isWindows: false,
    env: {}
  })

  assert.equal(home, '/Users/hermes/.hermes')
})
