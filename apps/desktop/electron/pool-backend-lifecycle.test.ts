import assert from 'node:assert/strict'

import { test } from 'vitest'

import { canStopIdlePoolBackend } from './pool-backend-lifecycle'

test('stale pool backend may be stopped when no turn is active', () => {
  assert.equal(
    canStopIdlePoolBackend({
      activeWorkCount: 0,
      idleForMs: 600_001,
      idleThresholdMs: 600_000
    }),
    true
  )
})

test('active work vetoes stopping a stale pool backend', () => {
  assert.equal(
    canStopIdlePoolBackend({
      activeWorkCount: 1,
      idleForMs: 600_001,
      idleThresholdMs: 600_000
    }),
    false
  )
})

test('fresh pool backend is retained when no turn is active', () => {
  assert.equal(
    canStopIdlePoolBackend({
      activeWorkCount: 0,
      idleForMs: 599_999,
      idleThresholdMs: 600_000
    }),
    false
  )
})
