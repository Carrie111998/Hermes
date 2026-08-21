import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  clearRegistryAuthScope,
  createDraftRegistryAuthScope,
  mintRegistryAuthTicket,
  promoteRegistryAuthScope,
  registryAuthPartition,
  registryAuthStorageKey,
  saveWithRegistryAuthPromotion,
  serializeRegistryAuthFailure,
  validateRegistryAuthScope,
  verifyTokenRegistryAuth
} from './registry-auth'

test('registry auth scopes accept durable connection ids and stable draft ids', () => {
  assert.equal(validateRegistryAuthScope('homelab-2'), 'homelab-2')
  const draft = createDraftRegistryAuthScope(() => '76f9d14d-2f10-4ccb-9bb8-089935501512')
  assert.equal(draft, 'draft-76f9d14d-2f10-4ccb-9bb8-089935501512')
  assert.equal(validateRegistryAuthScope(draft), draft)
})

test('registry auth scopes reject partition injection and reserved local identity', () => {
  for (const value of ['', 'local', '../other', 'same host', 'persist:shared', 'draft-not-a-uuid']) {
    assert.throws(() => validateRegistryAuthScope(value), /scope/i)
  }
})

test('same-host registered connections receive distinct persistent cookie partitions', () => {
  assert.equal(registryAuthPartition('gateway-a'), 'persist:hermes-registry-auth-gateway-a')
  assert.equal(registryAuthPartition('gateway-b'), 'persist:hermes-registry-auth-gateway-b')
  assert.notEqual(registryAuthPartition('gateway-a'), registryAuthPartition('gateway-b'))
})

test('native token ownership is connection scoped rather than URL scoped', () => {
  const url = 'https://gateway.example.test'
  assert.equal(registryAuthStorageKey('gateway-a', url), 'registry:gateway-a')
  assert.equal(registryAuthStorageKey('gateway-b', url), 'registry:gateway-b')
  assert.notEqual(registryAuthStorageKey('gateway-a', url), registryAuthStorageKey('gateway-b', url))
})

test('readiness and runtime mint separate tickets in the validated connection scope', async () => {
  const calls: string[] = []

  const mint = async (baseUrl: string, headers: Record<string, string>, scope: string) => {
    calls.push(`${scope}:${baseUrl}:${headers['X-Access']}`)

    return `ticket-${calls.length}`
  }

  const readiness = await mintRegistryAuthTicket(
    'gateway-a',
    'https://gateway.example.test',
    { 'X-Access': 'secret' },
    mint
  )

  const runtime = await mintRegistryAuthTicket(
    'gateway-a',
    'https://gateway.example.test',
    { 'X-Access': 'secret' },
    mint
  )

  assert.equal(readiness, 'ticket-1')
  assert.equal(runtime, 'ticket-2')
  assert.deepEqual(calls, [
    'gateway-a:https://gateway.example.test:secret',
    'gateway-a:https://gateway.example.test:secret'
  ])
})

test('auth rejection crosses IPC as a structured serializable result', () => {
  const rejection = Object.assign(new Error('session expired'), { statusCode: 401 })
  assert.deepEqual(serializeRegistryAuthFailure(rejection, 'Could not verify the gateway session.'), {
    error: 'Could not verify the gateway session.',
    kind: 'auth-required',
    ok: false
  })
})

test('transport failures stay distinct from auth rejection and expose sanitized fallback text', () => {
  const failure = serializeRegistryAuthFailure(new Error('socket timed out'), 'Gateway transport verification failed.')
  assert.deepEqual(failure, {
    error: 'Gateway transport verification failed.',
    kind: 'transport-error',
    ok: false
  })
})

test('removing or abandoning a scope clears only its cookie jar and native token key', async () => {
  const clearedCookies: string[] = []
  const clearedTokens: string[] = []
  await clearRegistryAuthScope('gateway-a', 'https://same.example.test:9119', {
    clearCookies: async scope => clearedCookies.push(scope),
    clearNativeTokens: key => clearedTokens.push(key)
  })
  assert.deepEqual(clearedCookies, ['gateway-a'])
  assert.deepEqual(clearedTokens, ['registry:gateway-a'])
})

test('failed draft promotion does not persist a ready-looking registry row', async () => {
  let saved = false

  await assert.rejects(
    saveWithRegistryAuthPromotion({
      baseUrl: 'https://gateway.example.test',
      connectionId: 'gateway-a',
      draftScope: 'draft-76f9d14d-2f10-4ccb-9bb8-089935501512',
      persist: () => {
        saved = true
      },
      promote: async () => {
        throw new Error('promotion failed')
      }
    }),
    /promotion failed/
  )
  assert.equal(saved, false)
})

test('candidate token readiness authenticates HTTP and the real websocket leg', async () => {
  const calls: string[] = []

  const result = await verifyTokenRegistryAuth({
    baseUrl: 'https://gateway.example.test',
    headers: { 'X-Access': 'secret' },
    probeWebSocket: async (url, headers) => {
      calls.push(`ws:${url}:${headers['X-Access']}`)

      return { ok: true }
    },
    readStatus: async (url, token, headers) => {
      calls.push(`http:${url}:${token}:${headers['X-Access']}`)

      return { version: '1.2.3' }
    },
    resolveWebSocketUrl: async (url, token) => `${url.replace('https:', 'wss:')}/api/ws?token=${token}`,
    token: 'candidate-secret'
  })

  assert.deepEqual(result, { version: '1.2.3' })
  assert.deepEqual(calls, [
    'http:https://gateway.example.test/api/status:candidate-secret:secret',
    'ws:wss://gateway.example.test/api/ws?token=candidate-secret:secret'
  ])
})

test('candidate token readiness rejects a failed websocket leg', async () => {
  await assert.rejects(
    verifyTokenRegistryAuth({
      baseUrl: 'https://gateway.example.test',
      headers: {},
      probeWebSocket: async () => ({ ok: false, reason: 'upgrade rejected' }),
      readStatus: async () => ({ version: '1.2.3' }),
      resolveWebSocketUrl: async () => 'wss://gateway.example.test/api/ws?token=candidate',
      token: 'candidate'
    }),
    /WebSocket readiness check failed: upgrade rejected/
  )
})

test('saving a draft promotes credentials into the durable connection scope then clears the draft', async () => {
  const calls: string[] = []
  const draft = 'draft-76f9d14d-2f10-4ccb-9bb8-089935501512'
  await promoteRegistryAuthScope(draft, 'homelab', 'https://same.example.test:9119', {
    clearCookies: async scope => calls.push(`clear-cookies:${scope}`),
    clearNativeTokens: key => calls.push(`clear-token:${key}`),
    copyCookies: async (from, to) => calls.push(`copy-cookies:${from}->${to}`),
    moveNativeTokens: (from, to) => calls.push(`move-token:${from}->${to}`)
  })
  assert.deepEqual(calls, [
    `copy-cookies:${draft}->homelab`,
    `move-token:registry:${draft}->registry:homelab`,
    `clear-cookies:${draft}`,
    `clear-token:registry:${draft}`
  ])
})
