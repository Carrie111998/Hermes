import assert from 'node:assert/strict'

import { test } from 'vitest'

import { decideBootstrapRecovery } from './bootstrap-recovery'

test('repair against a usable runtime soft-restarts without forcing the installer', () => {
  const plan = decideBootstrapRecovery({
    kind: 'repair',
    runtimeUsable: true,
    backendAlive: true,
    connectionReady: true
  })

  assert.equal(plan.forceInstaller, false)
  assert.equal(plan.teardownBackend, true)
  assert.match(plan.log, /soft-restarting without reinstall/)
})

test('repair against an unusable runtime still forces the installer', () => {
  const plan = decideBootstrapRecovery({
    kind: 'repair',
    runtimeUsable: false,
    backendAlive: false,
    connectionReady: false
  })

  assert.equal(plan.forceInstaller, true)
  assert.equal(plan.teardownBackend, true)
  assert.match(plan.log, /forcing reinstall/)
})

test('retry reuses a live ready backend instead of tearing it down', () => {
  const plan = decideBootstrapRecovery({
    kind: 'reset',
    runtimeUsable: true,
    backendAlive: true,
    connectionReady: true
  })

  assert.equal(plan.forceInstaller, false)
  assert.equal(plan.teardownBackend, false)
  assert.match(plan.log, /reusing live ready backend/)
})

test('retry tears down when the backend is dead or never became ready', () => {
  assert.equal(
    decideBootstrapRecovery({
      kind: 'reset',
      runtimeUsable: true,
      backendAlive: false,
      connectionReady: true
    }).teardownBackend,
    true
  )
  assert.equal(
    decideBootstrapRecovery({
      kind: 'reset',
      runtimeUsable: true,
      backendAlive: true,
      connectionReady: false
    }).teardownBackend,
    true
  )
})
