import assert from 'node:assert/strict'
import test from 'node:test'

import { normalizeConnectionList } from '../connections.js'

test('normalizes the desktop connection registry object', () => {
  const connections = [
    { id: 'local', label: 'This device' },
    { id: 'remote', label: 'Remote gateway' }
  ]

  assert.deepEqual(normalizeConnectionList({ version: 2, primary: 'local', connections }), connections)
})

test('preserves legacy bare connection arrays', () => {
  const connections = [{ id: 'local' }, { id: 'remote' }]
  assert.deepEqual(normalizeConnectionList(connections), connections)
})

test('falls back to an empty list for unsupported shapes', () => {
  assert.deepEqual(normalizeConnectionList(null), [])
  assert.deepEqual(normalizeConnectionList({}), [])
})
