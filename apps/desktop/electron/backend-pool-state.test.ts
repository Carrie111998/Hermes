import assert from 'node:assert/strict'

import { test } from 'vitest'

import { cleanupFailedBackendPoolEntry, deleteBackendPoolEntryIfCurrent } from './backend-pool-state'

test('a stale backend exit cannot remove the replacement pool entry', () => {
  const staleEntry = { id: 'stale' }
  const replacementEntry = { id: 'replacement' }
  const entries = new Map([['brainkit', replacementEntry]])

  assert.equal(deleteBackendPoolEntryIfCurrent(entries, 'brainkit', staleEntry), false)
  assert.equal(entries.get('brainkit'), replacementEntry)
})

test('the current backend exit removes its pool entry', () => {
  const entry = { id: 'current' }
  const entries = new Map([['brainkit', entry]])

  assert.equal(deleteBackendPoolEntryIfCurrent(entries, 'brainkit', entry), true)
  assert.equal(entries.has('brainkit'), false)
})

test('a failed stale startup stops its child without removing the replacement', () => {
  const staleEntry = { process: { id: 'stale-process' } }
  const replacementEntry = { process: { id: 'replacement-process' } }
  const entries = new Map([['brainkit', replacementEntry]])
  const stopped: Array<{ id: string }> = []

  cleanupFailedBackendPoolEntry(entries, 'brainkit', staleEntry, child => stopped.push(child))

  assert.deepEqual(stopped, [staleEntry.process])
  assert.equal(entries.get('brainkit'), replacementEntry)
})
