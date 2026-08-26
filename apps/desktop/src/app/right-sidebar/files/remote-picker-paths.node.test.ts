import assert from 'node:assert/strict'
import test from 'node:test'

import { cleanRemotePath, parentRemotePath, remotePathCrumbs, remotePathLeaf } from './remote-picker-paths'

test('keeps a Windows drive root navigable', () => {
  assert.equal(cleanRemotePath('C:\\'), 'C:')
  assert.equal(parentRemotePath('C:\\'), 'C:\\')
})

test('walks Windows paths without treating backslashes as filename text', () => {
  assert.equal(parentRemotePath('C:\\Users\\Steven'), 'C:\\Users')
  assert.equal(remotePathLeaf('C:\\Users\\Steven'), 'Steven')
  assert.deepEqual(remotePathCrumbs('C:\\Users\\Steven'), [
    { label: 'C:', path: 'C:\\' },
    { label: 'Users', path: 'C:\\Users' },
    { label: 'Steven', path: 'C:\\Users\\Steven' }
  ])
})

test('preserves POSIX navigation', () => {
  assert.equal(cleanRemotePath('/var/log/'), '/var/log')
  assert.equal(parentRemotePath('/var/log'), '/var')
  assert.equal(remotePathLeaf('/var/log'), 'log')
  assert.deepEqual(remotePathCrumbs('/var/log'), [
    { label: '/', path: '/' },
    { label: 'var', path: '/var' },
    { label: 'log', path: '/var/log' }
  ])
})
