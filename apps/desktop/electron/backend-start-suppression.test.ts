import assert from 'node:assert/strict'

import { test } from 'vitest'

import { backendStartSuppressionReason } from './backend-start-suppression'

test('allows a backend spawn when no update is in flight', () => {
  assert.equal(backendStartSuppressionReason(false), null)
})

test('suppresses a backend spawn while an update is in flight', () => {
  const reason = backendStartSuppressionReason(true)
  assert.ok(reason, 'expected a non-null suppression reason during an update')
  assert.match(reason as string, /update is in progress/i)
})
