import assert from 'node:assert/strict'

import { test } from 'vitest'

import { assertPoolRuntimeInstalled, POOL_LOCAL_RUNTIME_MISSING, poolBackendNeedsBootstrap } from './pool-runtime-guard'

test('poolBackendNeedsBootstrap detects the unresolved runtime', () => {
  assert.equal(poolBackendNeedsBootstrap({ kind: 'bootstrap-needed' }), true)
})

test('poolBackendNeedsBootstrap accepts a resolved runtime and tolerates junk', () => {
  assert.equal(poolBackendNeedsBootstrap({ kind: 'active' }), false)
  assert.equal(poolBackendNeedsBootstrap({}), false)
  assert.equal(poolBackendNeedsBootstrap(null), false)
  assert.equal(poolBackendNeedsBootstrap(undefined), false)
})

test('assertPoolRuntimeInstalled refuses to install for a pool backend', () => {
  assert.throws(() => assertPoolRuntimeInstalled({ kind: 'bootstrap-needed' }), {
    message: POOL_LOCAL_RUNTIME_MISSING
  })
})

test('assertPoolRuntimeInstalled is a no-op once a local runtime exists', () => {
  assert.doesNotThrow(() => assertPoolRuntimeInstalled({ kind: 'active' }))
})
