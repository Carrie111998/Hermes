import assert from 'node:assert/strict'

import { test } from 'vitest'

import { assertLocalBackendForRequest, resolveLocalitySensitiveBackend } from './api-backend-routing'

interface TestConnection {
  baseUrl: string
  mode: string
  token?: string
}

test('local-only request validates and transmits through the same pinned connection', async () => {
  const local: TestConnection = { baseUrl: 'http://127.0.0.1:9001', mode: 'local', token: 'local-token' }
  const remote: TestConnection = { baseUrl: 'https://remote.example', mode: 'remote', token: 'remote-token' }
  let configuredConnection = local
  let resolveCalls = 0

  const ensureBackend = async () => {
    resolveCalls += 1

    return configuredConnection
  }

  // This mirrors the locality-sensitive branch in main.ts: resolve once,
  // survive intervening awaits/config changes, validate that object, use it.
  const request = { profile: 'work', requireLocalBackend: true }
  const pinnedConnection = await resolveLocalitySensitiveBackend(request, ensureBackend)
  await Promise.resolve()
  configuredConnection = remote
  assertLocalBackendForRequest(request, pinnedConnection)
  const transmittedTo = pinnedConnection?.baseUrl

  assert.equal(resolveCalls, 1)
  assert.equal(transmittedTo, local.baseUrl)
  assert.notEqual(transmittedTo, configuredConnection.baseUrl)
})

test('local-only request fails closed when its exact pinned connection is remote', () => {
  assert.throws(
    () =>
      assertLocalBackendForRequest({ requireLocalBackend: true }, { baseUrl: 'https://remote.example', mode: 'remote' }),
    /requires a local Hermes backend/
  )
})

test('ordinary requests keep normal routing and never pin a backend', async () => {
  let resolveCalls = 0

  const ensureBackend = async () => {
    resolveCalls += 1

    return { baseUrl: 'https://remote.example', mode: 'remote' }
  }

  assert.equal(await resolveLocalitySensitiveBackend({ profile: 'work' }, ensureBackend), null)
  assert.equal(await resolveLocalitySensitiveBackend(undefined, ensureBackend), null)
  assert.equal(resolveCalls, 0)
  // A remote connection is fine when locality was never demanded.
  assertLocalBackendForRequest({ profile: 'work' }, { baseUrl: 'https://remote.example', mode: 'remote' })
})
