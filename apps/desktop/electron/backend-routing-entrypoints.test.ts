/**
 * Tests for the exact backend-routing entry-point seam that main.ts uses.
 *
 * Run via the vitest electron project.
 */

import assert from 'node:assert/strict'

import { test } from 'vitest'

import { resolveExplicitBackendRail, touchPooledBackendEntries } from './backend-routing-entrypoints'

test('touchPooledBackendEntries refreshes the exact pooled root variants main.ts can own', () => {
  const pool = new Map([
    ['default', { lastActiveAt: 1 }],
    ['local:default', { lastActiveAt: 2 }],
    ['remote:default', { lastActiveAt: 3 }],
    ['writer', { lastActiveAt: 4 }]
  ])

  touchPooledBackendEntries(pool, 'default', 99)

  assert.equal(pool.get('default')?.lastActiveAt, 99)
  assert.equal(pool.get('local:default')?.lastActiveAt, 99)
  assert.equal(pool.get('remote:default')?.lastActiveAt, 99)
  assert.equal(pool.get('writer')?.lastActiveAt, 4)
})

test('resolveExplicitBackendRail keeps a saved inactive SSH global backend for remote-only routing', () => {
  assert.deepEqual(
    resolveExplicitBackendRail(
      {
        mode: 'local',
        remote: {
          mode: 'ssh',
          host: 'devbox.internal',
          user: 'operator',
          port: 2222,
          keyPath: '/redacted/key',
          remoteProfile: 'default',
          token: { encrypted: 'redacted-token' }
        }
      },
      { remoteOnly: true }
    ),
    {
      kind: 'ssh',
      remoteKind: 'ssh',
      ssh: {
        mode: 'ssh',
        host: 'devbox.internal',
        user: 'operator',
        port: 2222,
        keyPath: '/redacted/key',
        remoteProfile: 'default'
      },
      tokenRef: { encrypted: 'redacted-token' }
    }
  )
})
