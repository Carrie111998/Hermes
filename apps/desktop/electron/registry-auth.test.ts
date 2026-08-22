import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { test } from 'vitest'

import {
  authenticatedRegistryStatus,
  clearRegistryAuthCredentialsTransactionally,
  clearRegistryAuthScope,
  createDraftRegistryAuthScope,
  createRegistryAuthTargetConnectionId,
  mintRegistryAuthTicket,
  promoteAndPersistRegistryAuth,
  promoteRegistryAuthScope,
  readDurableRegistryAuthStatus,
  registryAuthCandidateBinding,
  RegistryAuthCleanupRetryQueue,
  type RegistryAuthCredentialSnapshot,
  registryAuthPartition,
  RegistryAuthReadinessAuthority,
  registryAuthReadinessRequired,
  registryAuthStorageKey,
  removeRegistryConnectionTransactionally,
  resolveRegistryAuthCandidateHeaders,
  resolveRegistryAuthScopeAuthority,
  revokeRegistryAuthScope,
  runStructuredRegistryTest,
  saveVerifiedRegistryConnection,
  saveWithRegistryAuthPromotion,
  serializeRegistryAuthFailure,
  teardownRemovedRegistryConnection,
  testAuthenticatedRegistryConnection,
  validateRegistryAuthScope,
  verifyTokenRegistryAuth
} from './registry-auth'

function readinessBinding(overrides: Partial<Parameters<typeof registryAuthCandidateBinding>[0]> = {}) {
  return registryAuthCandidateBinding({
    authMode: 'oauth',
    baseUrl: 'https://gateway.example.test:9119/',
    connectionId: 'gateway-a',
    generation: 3,
    headers: { 'X-Access': 'secret' },
    scope: 'gateway-a',
    token: '',
    ...overrides
  })
}

test('readiness capability is accepted exactly once', () => {
  let now = 1_000

  const authority = new RegistryAuthReadinessAuthority({
    now: () => now,
    randomToken: () => 'capability-1',
    ttlMs: 60_000
  })

  const binding = readinessBinding()
  const capability = authority.issue(binding)

  assert.equal(capability, 'capability-1')
  authority.consume(capability, binding)
  assert.throws(() => authority.consume(capability, binding), /used|unknown/i)
  now += 1
})

test('readiness capability rejects missing and expired proof', () => {
  let now = 1_000

  const authority = new RegistryAuthReadinessAuthority({
    now: () => now,
    randomToken: () => 'capability-1',
    ttlMs: 60_000
  })

  const binding = readinessBinding()

  assert.throws(() => authority.consume(undefined, binding), /readiness/i)
  const capability = authority.issue(binding)
  now += 60_000
  assert.throws(() => authority.consume(capability, binding), /expired/i)
})

test('readiness capability binds every canonical candidate field and burns on mismatch', () => {
  const changedBindings = [
    readinessBinding({ scope: 'gateway-b' }),
    readinessBinding({ baseUrl: 'https://gateway.example.test:9120' }),
    readinessBinding({ authMode: 'token' }),
    readinessBinding({ headers: { 'X-Access': 'different' } }),
    readinessBinding({ token: 'different' }),
    readinessBinding({ connectionId: 'gateway-b' }),
    readinessBinding({ generation: 4 })
  ]

  for (const [index, changed] of changedBindings.entries()) {
    const capability = `capability-${index}`
    const authority = new RegistryAuthReadinessAuthority({ randomToken: () => capability })
    const original = readinessBinding()
    authority.issue(original)
    assert.throws(() => authority.consume(capability, changed), /does not match/i)
    assert.throws(() => authority.consume(capability, original), /used|unknown/i)
  }
})

test('draft scope authority binds verification and save to its native target connection id', async () => {
  const draft = 'draft-76f9d14d-2f10-4ccb-9bb8-089935501512'
  const targetConnectionId = createRegistryAuthTargetConnectionId(() => '76f9d14d-2f10-4ccb-9bb8-089935501512')
  const authority = new RegistryAuthReadinessAuthority({ randomToken: () => 'capability-1' })
  authority.registerDraft(draft, targetConnectionId)

  const scopeAuthority = resolveRegistryAuthScopeAuthority({
    authority,
    baseUrl: 'https://gateway.example.test:9119',
    registry: { connections: [] },
    scope: draft
  })

  const binding = readinessBinding({ scope: draft, connectionId: targetConnectionId })
  const capability = authority.issue(binding)

  assert.equal(scopeAuthority.connectionId, targetConnectionId)
  await assert.rejects(
    saveVerifiedRegistryConnection({
      authority,
      binding,
      capability,
      connectionId: 'renderer-chosen-id',
      persist: () => undefined
    }),
    /target|connection/i
  )
})

test('invalidating a scope revokes its capabilities and draft authority', () => {
  const draft = 'draft-76f9d14d-2f10-4ccb-9bb8-089935501512'
  const authority = new RegistryAuthReadinessAuthority({ randomToken: () => 'capability-1' })
  authority.registerDraft(draft, 'gateway-a')
  const binding = readinessBinding({ scope: draft, connectionId: 'gateway-a' })
  const capability = authority.issue(binding)

  assert.equal(authority.ownsDraft(draft, 'gateway-a'), true)
  authority.invalidateScope(draft)
  assert.equal(authority.ownsDraft(draft, 'gateway-a'), false)
  assert.throws(() => authority.consume(capability, binding), /used|unknown/i)
})

test('label-only existing remote save preserves auth without consuming readiness', async () => {
  const existing = {
    authMode: 'token' as const,
    headers: { 'X-Access': { encrypted: 'saved-header' } },
    id: 'gateway-a',
    kind: 'remote' as const,
    label: 'Old label',
    token: { encrypted: 'saved-token' },
    url: 'https://gateway.example.test:9119'
  }

  const renamed = { ...existing, label: 'New label' }
  const authority = new RegistryAuthReadinessAuthority()
  let persisted = false

  assert.equal(registryAuthReadinessRequired(existing, renamed), false)
  await saveVerifiedRegistryConnection({
    authority,
    binding: readinessBinding(),
    capability: undefined,
    connectionId: renamed.id,
    persist: () => {
      persisted = true
    },
    readinessRequired: false
  })

  assert.equal(persisted, true)
  assert.deepEqual(renamed.token, existing.token)
  assert.deepEqual(renamed.headers, existing.headers)
})

test('label-only save rejects draft promotion without matching readiness', async () => {
  const authority = new RegistryAuthReadinessAuthority()

  const binding = readinessBinding({
    connectionId: 'gateway-a',
    scope: 'draft-76f9d14d-2f10-4ccb-9bb8-089935501512'
  })

  let persisted = false
  let promoted = false

  await assert.rejects(
    saveVerifiedRegistryConnection({
      authority,
      binding,
      capability: undefined,
      connectionId: 'gateway-a',
      persist: () => {
        persisted = true
      },
      promote: async () => {
        promoted = true
      },
      readinessRequired: false
    }),
    /readiness/i
  )

  assert.equal(promoted, false)
  assert.equal(persisted, false)
})

test('verified save rejects missing and stale readiness capabilities before persistence', async () => {
  const authority = new RegistryAuthReadinessAuthority({ randomToken: () => 'capability-1' })
  const binding = readinessBinding()
  let persisted = false

  const deps = {
    authority,
    binding,
    connectionId: binding.connectionId,
    persist: () => {
      persisted = true
    }
  }

  await assert.rejects(saveVerifiedRegistryConnection({ capability: undefined, ...deps }), /readiness/i)
  const staleCapability = authority.issue(readinessBinding({ generation: 2 }))
  await assert.rejects(saveVerifiedRegistryConnection({ capability: staleCapability, ...deps }), /readiness|match/i)
  assert.equal(persisted, false)
})

test('durable scope authority rejects caller URL mismatch, orphan ids, and same-host port changes', () => {
  const authority = new RegistryAuthReadinessAuthority()

  const registry = {
    connections: [
      { id: 'gateway-a', kind: 'remote', url: 'https://gateway.example.test:9119' },
      { id: 'gateway-b', kind: 'remote', url: 'https://gateway.example.test:9120' }
    ]
  }

  assert.throws(
    () => resolveRegistryAuthScopeAuthority({ authority, baseUrl: registry.connections[1].url, registry, scope: 'gateway-a' }),
    /match|scope|url/i
  )
  assert.throws(
    () => resolveRegistryAuthScopeAuthority({ authority, baseUrl: registry.connections[0].url, registry, scope: 'orphan' }),
    /registered|remote/i
  )
  assert.throws(
    () => resolveRegistryAuthScopeAuthority({ authority, baseUrl: 'https://gateway.example.test:9120', registry, scope: 'gateway-a' }),
    /match|scope|url/i
  )
})

test('candidate binding normalizes URL and fingerprints secrets canonically', () => {
  const first = readinessBinding({ headers: { 'X-Z': 'last', 'x-a': 'first' } })

  const second = readinessBinding({
    baseUrl: 'https://gateway.example.test:9119',
    headers: { 'X-A': 'first', 'x-z': 'last' }
  })

  assert.deepEqual(first, second)
  assert.equal(JSON.stringify(first).includes('first'), false)
  assert.equal(JSON.stringify(first).includes('last'), false)
})

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
    ok: false,
    statusCode: 401
  })
})

test('transport failures stay distinct from auth rejection and expose sanitized fallback text', () => {
  const failure = serializeRegistryAuthFailure(new Error('socket timed out'), 'Gateway transport verification failed.')
  assert.deepEqual(failure, {
    error: 'Gateway transport verification failed.',
    kind: 'transport-error',
    ok: false,
    statusCode: null
  })
})

test('removing a connection invalidates its generation and outstanding capabilities', async () => {
  const authority = new RegistryAuthReadinessAuthority({ randomToken: () => 'capability-1' })
  const binding = readinessBinding()
  const capability = authority.issue(binding)

  await revokeRegistryAuthScope('gateway-a', 'https://gateway.example.test:9119', authority, {
    clearCookies: async () => undefined,
    clearNativeTokens: () => undefined
  })

  assert.equal(authority.generationForScope('gateway-a'), 1)
  assert.throws(() => authority.consume(capability, binding), /used|unknown/i)
})

test('removal revokes authority even when backend teardown fails after deletion', async () => {
  const authority = new RegistryAuthReadinessAuthority({ randomToken: () => 'capability-1' })
  const binding = readinessBinding()
  const capability = authority.issue(binding)
  let credentialClears = 0

  await assert.rejects(
    teardownRemovedRegistryConnection({
      authority,
      clearCredentials: async () => {
        credentialClears += 1
      },
      scope: 'gateway-a',
      stopBackends: async () => {
        throw new Error('backend stop failed')
      }
    }),
    /backend stop failed/
  )

  assert.equal(credentialClears, 1)
  assert.equal(authority.generationForScope('gateway-a'), 1)
  assert.throws(() => authority.consume(capability, binding), /used|unknown/i)
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

type FakeCredentialState = { cookies: string[]; nativeTokens: null | string }
type FakeCredentialFailure =
  | 'destination-snapshot'
  | 'destination-clear'
  | 'cookie-copy'
  | 'native-token-copy'
  | 'source-clear'

type FakeCredentialStore = ReturnType<typeof createFakeCredentialStore>

function createFakeCredentialStore(failAt?: FakeCredentialFailure) {
  let failure = failAt
  const draft = 'draft-76f9d14d-2f10-4ccb-9bb8-089935501512'
  const destination = 'gateway-a'

  const states = new Map<string, FakeCredentialState>([
    [draft, { cookies: ['draft-cookie'], nativeTokens: 'draft-token' }],
    [destination, { cookies: ['old-cookie'], nativeTokens: 'old-token' }]
  ])

  const events: string[] = []
  const read = (scope: string): FakeCredentialState => structuredClone(states.get(scope)!)
  const write = (scope: string, state: FakeCredentialState) => states.set(scope, structuredClone(state))

  return {
    destination,
    draft,
    events,
    lifecycle: {
      snapshot: async (scope: string) => {
        events.push(`snapshot:${scope}`)

        if (scope === destination && failure === 'destination-snapshot') {throw new Error('destination snapshot failed')}

        return read(scope)
      },
      replace: async (fromScope: string, toScope: string) => {
        const source = read(fromScope)
        events.push(`clear:${toScope}`)
        write(toScope, { cookies: [], nativeTokens: null })

        if (failure === 'destination-clear') {throw new Error('destination clear failed')}
        events.push(`copy-cookies:${fromScope}->${toScope}`)

        if (failure === 'cookie-copy') {throw new Error('cookie copy failed')}
        write(toScope, { ...read(toScope), cookies: [...source.cookies] })
        events.push(`copy-native:${fromScope}->${toScope}`)

        if (failure === 'native-token-copy') {throw new Error('native token copy failed')}
        write(toScope, { ...read(toScope), nativeTokens: source.nativeTokens })
      },
      restore: async (scope: string, _baseUrl: string, snapshot: RegistryAuthCredentialSnapshot) => {
        events.push(`restore:${scope}`)
        write(scope, snapshot as FakeCredentialState)
      },
      clear: async (scope: string) => {
        events.push(`clear:${scope}`)

        if (scope === draft && failure === 'source-clear') {throw new Error('source clear failed')}
        write(scope, { cookies: [], nativeTokens: null })
      }
    },
    allowFailures: () => { failure = undefined },
    read
  }
}

async function attemptFakePromotion(store: FakeCredentialStore, persist: () => unknown = () => undefined) {
  return promoteAndPersistRegistryAuth({
    baseUrl: 'https://gateway.example.test',
    fromScope: store.draft,
    toScope: store.destination,
    lifecycle: store.lifecycle,
    persist
  })
}

test('destination snapshot failure leaves both scopes untouched and skips registry persistence', async () => {
  const store = createFakeCredentialStore('destination-snapshot')
  const destinationBefore = store.read(store.destination)
  const sourceBefore = store.read(store.draft)
  let registryWritten = false

  await assert.rejects(attemptFakePromotion(store, () => { registryWritten = true }), /destination snapshot failed/)

  assert.deepEqual(store.read(store.destination), destinationBefore)
  assert.deepEqual(store.read(store.draft), sourceBefore)
  assert.equal(registryWritten, false)
})

test('destination clear failure restores previous durable credentials and skips registry persistence', async () => {
  const store = createFakeCredentialStore('destination-clear')
  const destinationBefore = store.read(store.destination)
  const sourceBefore = store.read(store.draft)
  let registryWritten = false

  await assert.rejects(attemptFakePromotion(store, () => { registryWritten = true }), /destination clear failed/)

  assert.deepEqual(store.read(store.destination), destinationBefore)
  assert.deepEqual(store.read(store.draft), sourceBefore)
  assert.equal(registryWritten, false)
  assert.ok(store.events.includes(`restore:${store.destination}`))
})

test('cookie-copy failure restores the exact destination snapshot and retains source credentials', async () => {
  const store = createFakeCredentialStore('cookie-copy')
  const destinationBefore = store.read(store.destination)
  const sourceBefore = store.read(store.draft)
  let registryWritten = false

  await assert.rejects(attemptFakePromotion(store, () => { registryWritten = true }), /cookie copy failed/)

  assert.ok(
    store.events.includes(`copy-cookies:${store.draft}->${store.destination}`),
    'the injected cookie-copy boundary must execute'
  )
  assert.deepEqual(store.read(store.destination), destinationBefore)
  assert.deepEqual(store.read(store.draft), sourceBefore)
  assert.equal(registryWritten, false)
})

test('native-token-copy failure rolls back cookies and tokens to the exact destination snapshot', async () => {
  const store = createFakeCredentialStore('native-token-copy')
  const destinationBefore = store.read(store.destination)
  const sourceBefore = store.read(store.draft)
  let registryWritten = false

  await assert.rejects(attemptFakePromotion(store, () => { registryWritten = true }), /native token copy failed/)

  assert.ok(
    store.events.includes(`copy-native:${store.draft}->${store.destination}`),
    'the injected native-token-copy boundary must execute'
  )
  assert.deepEqual(store.read(store.destination), destinationBefore)
  assert.deepEqual(store.read(store.draft), sourceBefore)
  assert.equal(registryWritten, false)
})

test('registry persistence failure restores destination and retains draft credentials', async () => {
  const store = createFakeCredentialStore()
  const destinationBefore = store.read(store.destination)
  const sourceBefore = store.read(store.draft)

  await assert.rejects(
    attemptFakePromotion(store, () => {
      store.events.push('persist')
      throw new Error('registry write failed')
    }),
    /registry write failed/
  )

  assert.deepEqual(store.read(store.destination), destinationBefore)
  assert.deepEqual(store.read(store.draft), sourceBefore)
  assert.ok(store.events.includes(`restore:${store.destination}`))
})

test('promotion preserves simultaneous registry-write and destination-restore failures as uncertain state', async () => {
  const primary = new Error('registry write failed')
  const rollback = new Error('destination restore failed')
  const store = createFakeCredentialStore()
  let restoreInjected = false

  const lifecycle = {
    ...store.lifecycle,
    restore: async () => {
      restoreInjected = true
      throw rollback
    }
  }

  await assert.rejects(
    promoteAndPersistRegistryAuth({
      baseUrl: 'https://gateway.example.test',
      fromScope: store.draft,
      lifecycle,
      persist: () => {
        throw primary
      },
      toScope: store.destination
    }),
    error => {
      assert.ok(error instanceof AggregateError)
      assert.deepEqual(error.errors, [primary, rollback])
      assert.equal((error as AggregateError & { stateUncertain?: boolean }).stateUncertain, true)

      return true
    }
  )
  assert.equal(restoreInjected, true, 'the injected rollback failure must execute')
})

test('promotion rethrows a lone registry-write failure unchanged after successful restore', async () => {
  const primary = new Error('registry write failed')
  const store = createFakeCredentialStore()
  let restoreExecuted = false

  const lifecycle = {
    ...store.lifecycle,
    restore: async (...args: Parameters<typeof store.lifecycle.restore>) => {
      restoreExecuted = true
      await store.lifecycle.restore(...args)
    }
  }

  await assert.rejects(
    promoteAndPersistRegistryAuth({
      baseUrl: 'https://gateway.example.test',
      fromScope: store.draft,
      lifecycle,
      persist: () => {
        throw primary
      },
      toScope: store.destination
    }),
    error => error === primary
  )
  assert.equal(restoreExecuted, true, 'the successful restore hook must execute')
})

test('transactional credential clear preserves simultaneous clear and restore failures as uncertain state', async () => {
  const primary = new Error('credential clear failed')
  const rollback = new Error('credential restore failed')
  let clearInjected = false
  let restoreInjected = false

  await assert.rejects(
    clearRegistryAuthCredentialsTransactionally({
      clear: async () => {
        clearInjected = true
        throw primary
      },
      restore: async () => {
        restoreInjected = true
        throw rollback
      },
      snapshot: async () => ({ cookies: [], nativeTokens: undefined })
    }),
    error => {
      assert.ok(error instanceof AggregateError)
      assert.deepEqual(error.errors, [primary, rollback])
      assert.equal((error as AggregateError & { stateUncertain?: boolean }).stateUncertain, true)

      return true
    }
  )
  assert.equal(clearInjected, true, 'the injected clear failure must execute')
  assert.equal(restoreInjected, true, 'the injected restore failure must execute')
})

test('transactional credential clear rethrows a lone clear failure unchanged after successful restore', async () => {
  const primary = new Error('credential clear failed')
  let restoreExecuted = false

  await assert.rejects(
    clearRegistryAuthCredentialsTransactionally({
      clear: async () => {
        throw primary
      },
      restore: async () => {
        restoreExecuted = true
      },
      snapshot: async () => ({ cookies: [] })
    }),
    error => error === primary
  )
  assert.equal(restoreExecuted, true, 'the successful restore hook must execute')
})

test('successful promotion replaces destination exactly, persists before clearing source, and then clears draft', async () => {
  const store = createFakeCredentialStore()
  const sourceBefore = store.read(store.draft)

  const result = await attemptFakePromotion(store, () => {
    store.events.push('persist')

    return 'saved'
  })

  assert.equal(result, 'saved')
  assert.deepEqual(store.read(store.destination), sourceBefore)
  assert.deepEqual(store.read(store.draft), { cookies: [], nativeTokens: null })
  assert.ok(store.events.indexOf('persist') < store.events.lastIndexOf(`clear:${store.draft}`))
})

test('source cleanup failure after persistence keeps durable credentials and queues a retry', async () => {
  const store = createFakeCredentialStore('source-clear')
  const sourceBefore = store.read(store.draft)
  const retries = new RegistryAuthCleanupRetryQueue()

  const result = await promoteAndPersistRegistryAuth({
    baseUrl: 'https://gateway.example.test',
    cleanupRetries: retries,
    fromScope: store.draft,
    toScope: store.destination,
    lifecycle: store.lifecycle,
    persist: () => {
      store.events.push('persist')

      return 'saved'
    }
  })

  assert.equal(result, 'saved')
  assert.deepEqual(store.read(store.destination), sourceBefore)
  assert.deepEqual(store.read(store.draft), sourceBefore)
  assert.equal(retries.size, 1)
  assert.equal(store.events.includes(`restore:${store.destination}`), false)

  store.allowFailures()
  await retries.retry(store.lifecycle)
  assert.deepEqual(store.read(store.draft), { cookies: [], nativeTokens: null })
  assert.equal(retries.size, 0)
})

test('source cleanup failures are reported while remaining retryable', async () => {
  const store = createFakeCredentialStore('source-clear')
  const retries = new RegistryAuthCleanupRetryQueue()
  const failures: unknown[] = []

  await promoteAndPersistRegistryAuth({
    baseUrl: 'https://gateway.example.test',
    cleanupRetries: retries,
    fromScope: store.draft,
    lifecycle: store.lifecycle,
    onCleanupFailure: error => failures.push(error),
    persist: () => 'saved',
    toScope: store.destination
  })

  assert.equal(retries.size, 1)
  assert.match(String(failures[0]), /source clear failed/)

  await retries.retry(store.lifecycle, error => failures.push(error))
  assert.equal(retries.size, 1)
  assert.equal(failures.length, 2)
})

const __dirname = path.dirname(fileURLToPath(import.meta.url))

function readMainSource(): string {
  return fs.readFileSync(path.join(__dirname, 'main.ts'), 'utf8').replace(/\r\n/g, '\n')
}

function mainSourceSlice(startMarker: string, endMarker: string): string {
  const source = readMainSource()
  const start = source.indexOf(startMarker)
  const end = source.indexOf(endMarker, start + startMarker.length)
  assert.notEqual(start, -1, `${startMarker} should exist in main.ts`)
  assert.notEqual(end, -1, `${endMarker} should follow ${startMarker} in main.ts`)

  return source.slice(start, end)
}

test('authoritative header candidates retain only explicit null entries and replace renamed values', () => {
  const stored = { 'CF-Access-Client-Secret': 'stored-secret', 'X-Old-Access': 'old-secret' }

  assert.deepEqual(resolveRegistryAuthCandidateHeaders({}, stored), {})
  assert.deepEqual(resolveRegistryAuthCandidateHeaders({ 'CF-Access-Client-Secret': null }, stored), {
    'CF-Access-Client-Secret': 'stored-secret'
  })
  assert.deepEqual(resolveRegistryAuthCandidateHeaders({ 'X-New-Access': 'replacement-secret' }, stored), {
    'X-New-Access': 'replacement-secret'
  })
})

test('authenticated durable status uses the exact scoped native bearer and candidate headers', async () => {
  const calls: unknown[][] = []

  const status = await authenticatedRegistryStatus(
    {
      authMode: 'oauth',
      baseUrl: 'https://gateway.example.test',
      headers: { 'CF-Access-Client-Secret': 'candidate-secret' },
      scope: 'gateway-a'
    },
    {
      readNativeAccessToken: async (baseUrl, scope) => {
        calls.push(['native', baseUrl, scope])

        return 'native-access-token'
      },
      readBearerStatus: async (url, bearer, headers) => {
        calls.push(['bearer', url, bearer, headers])

        return { version: '0.20.4' }
      },
      readOauthStatus: async () => {
        throw new Error('cookie status must not run when native auth is available')
      },
      readTokenStatus: async () => {
        throw new Error('token status must not run for OAuth')
      }
    }
  )

  assert.deepEqual(status, { version: '0.20.4' })
  assert.deepEqual(calls, [
    ['native', 'https://gateway.example.test', 'gateway-a'],
    [
      'bearer',
      'https://gateway.example.test/api/status',
      'native-access-token',
      { 'CF-Access-Client-Secret': 'candidate-secret' }
    ]
  ])
})

test('authenticated durable status uses the exact scoped OAuth cookie session when native auth is absent', async () => {
  const calls: unknown[][] = []
  await authenticatedRegistryStatus(
    { authMode: 'oauth', baseUrl: 'https://gateway.example.test', headers: {}, scope: 'gateway-a' },
    {
      readNativeAccessToken: async () => null,
      readBearerStatus: async () => {
        throw new Error('bearer status must not run without native auth')
      },
      readOauthStatus: async (url, scope, headers) => {
        calls.push([url, scope, headers])

        return { version: 'cookie' }
      },
      readTokenStatus: async () => {
        throw new Error('token status must not run for OAuth')
      }
    }
  )

  assert.deepEqual(calls, [['https://gateway.example.test/api/status', 'gateway-a', {}]])
})

test('durable authenticated status classifies revoked bearer and cookie sessions as auth-required', async () => {
  for (const nativeToken of ['revoked-native-token', null] as const) {
    const calls: string[] = []

    const result = await readDurableRegistryAuthStatus(
      { authMode: 'oauth', baseUrl: 'https://gateway.example.test', headers: {}, scope: 'gateway-a' },
      {
        readNativeAccessToken: async () => nativeToken,
        readBearerStatus: async () => {
          calls.push('bearer')
          throw Object.assign(new Error('revoked bearer'), { statusCode: 401 })
        },
        readOauthStatus: async () => {
          calls.push('cookie')
          throw Object.assign(new Error('revoked cookie'), { statusCode: 403 })
        },
        readTokenStatus: async () => {
          throw new Error('token status must not run for OAuth')
        }
      }
    )

    assert.deepEqual(result, {
      error: 'Could not read gateway auth status.',
      kind: 'auth-required',
      ok: false,
      statusCode: nativeToken ? 401 : 403
    })
    assert.deepEqual(calls, [nativeToken ? 'bearer' : 'cookie'])
  }
})

test('durable authenticated status classifies 5xx and timeout failures as transport errors', async () => {
  const failures = [
    Object.assign(new Error('upstream unavailable'), { statusCode: 503 }),
    new Error('Timed out connecting to Hermes backend after 8000ms')
  ]

  for (const failure of failures) {
    let statusCalls = 0

    const result = await readDurableRegistryAuthStatus(
      {
        authMode: 'token',
        baseUrl: 'https://gateway.example.test',
        headers: { 'X-Access': 'secret' },
        scope: 'gateway-a',
        token: 'durable-token'
      },
      {
        readNativeAccessToken: async () => null,
        readBearerStatus: async () => ({ version: 'unexpected' }),
        readOauthStatus: async () => ({ version: 'unexpected' }),
        readTokenStatus: async () => {
          statusCalls += 1
          throw failure
        }
      }
    )

    assert.equal(statusCalls, 1, 'the authenticated durable status hook must execute')
    assert.deepEqual(result, {
      error: 'Could not read gateway auth status.',
      kind: 'transport-error',
      ok: false,
      statusCode: 'statusCode' in failure ? 503 : null
    })
  }
})

test('Registry Test runs authenticated HTTP before the live WebSocket leg', async () => {
  const calls: unknown[][] = []

  const result = await testAuthenticatedRegistryConnection(
    {
      authMode: 'token',
      baseUrl: 'https://gateway.example.test',
      headers: { 'X-Access': 'secret' },
      scope: 'gateway-a',
      token: 'durable-token'
    },
    {
      onStatus: status => {
        calls.push(['status', status])
      },
      readNativeAccessToken: async () => null,
      readBearerStatus: async () => ({ version: 'unexpected' }),
      readOauthStatus: async () => ({ version: 'unexpected' }),
      readTokenStatus: async (url, token, headers) => {
        calls.push(['http', url, token, headers])

        return { version: '0.20.4' }
      },
      resolveWebSocketUrl: async (baseUrl, authMode, token) => {
        calls.push(['resolve-ws', baseUrl, authMode, token])

        return 'wss://gateway.example.test/api/ws?token=durable-token'
      },
      probeWebSocket: async (url, headers) => {
        calls.push(['ws', url, headers])

        return { ok: true }
      }
    }
  )

  assert.deepEqual(result, { baseUrl: 'https://gateway.example.test', ok: true, version: '0.20.4' })
  assert.deepEqual(calls, [
    ['http', 'https://gateway.example.test/api/status', 'durable-token', { 'X-Access': 'secret' }],
    ['status', { version: '0.20.4' }],
    ['resolve-ws', 'https://gateway.example.test', 'token', 'durable-token'],
    ['ws', 'wss://gateway.example.test/api/ws?token=durable-token', { 'X-Access': 'secret' }]
  ])
})

test('Registry Test returns structured auth failure and skips WebSocket after HTTP rejection', async () => {
  let httpCalls = 0
  let wsCalls = 0

  const result = await testAuthenticatedRegistryConnection(
    { authMode: 'oauth', baseUrl: 'https://gateway.example.test', headers: {}, scope: 'gateway-a' },
    {
      readNativeAccessToken: async () => 'revoked-token',
      readBearerStatus: async () => {
        httpCalls += 1
        throw Object.assign(new Error('revoked'), { statusCode: 401 })
      },
      readOauthStatus: async () => ({ version: 'unexpected' }),
      readTokenStatus: async () => ({ version: 'unexpected' }),
      resolveWebSocketUrl: async () => {
        wsCalls += 1

        return 'wss://gateway.example.test/api/ws'
      },
      probeWebSocket: async () => {
        wsCalls += 1

        return { ok: true }
      }
    }
  )

  assert.equal(httpCalls, 1, 'the authenticated HTTP hook must execute')
  assert.equal(wsCalls, 0)
  assert.deepEqual(result, {
    error: 'Could not test the registered gateway connection.',
    kind: 'auth-required',
    ok: false,
    statusCode: 401
  })
})

test('Registry Test returns structured transport failure when WebSocket fails after HTTP success', async () => {
  const calls: string[] = []

  const result = await testAuthenticatedRegistryConnection(
    { authMode: 'token', baseUrl: 'https://gateway.example.test', headers: {}, scope: 'gateway-a', token: 'token' },
    {
      readNativeAccessToken: async () => null,
      readBearerStatus: async () => ({ version: 'unexpected' }),
      readOauthStatus: async () => ({ version: 'unexpected' }),
      readTokenStatus: async () => {
        calls.push('http')

        return { version: '0.20.4' }
      },
      resolveWebSocketUrl: async () => {
        calls.push('resolve-ws')

        return 'wss://gateway.example.test/api/ws'
      },
      probeWebSocket: async () => {
        calls.push('ws')

        return { ok: false, reason: 'upgrade rejected' }
      }
    }
  )

  assert.deepEqual(calls, ['http', 'resolve-ws', 'ws'])
  assert.deepEqual(result, {
    error: 'Could not test the registered gateway connection.',
    kind: 'transport-error',
    ok: false,
    statusCode: null
  })
})

test('structured registry Test catches auth, 5xx, and timeout failures', async () => {
  const cases = [
    [Object.assign(new Error('revoked'), { statusCode: 401 }), 'auth-required', 401],
    [Object.assign(new Error('upstream unavailable'), { statusCode: 503 }), 'transport-error', 503],
    [new Error('Timed out connecting to Hermes backend after 8000ms'), 'transport-error', null]
  ] as const

  for (const [failure, kind, statusCode] of cases) {
    const result = await runStructuredRegistryTest(async () => {
      throw failure
    }, 'Connection test failed.')

    assert.deepEqual(result, { error: 'Connection test failed.', kind, ok: false, statusCode })
  }
})

test('Electron save wiring uses the atomic lifecycle and drains deferred cleanup', () => {
  const save = mainSourceSlice('async function saveRegistryConnection(', '\nfunction readActiveDesktopProfile(')

  assert.match(save, /registryAuthCleanupRetries\.retry\(lifecycle/)
  assert.match(save, /promoteAndPersistRegistryAuth\(\{/)
  assert.match(save, /fromScope: draftScope/)
  assert.match(save, /toScope: entry\.id/)
  assert.doesNotMatch(save, /promoteRegistryAuthScope\(/)
})

test('removal cleanup failure leaves the registry row intact', async () => {
  const row = { id: 'gateway-a' }
  const rows = [row]
  let clearInjected = false
  let persistCalled = false

  await assert.rejects(
    removeRegistryConnectionTransactionally({
      credentials: {
        clear: async () => {
          clearInjected = true
          throw new Error('credential cleanup failed')
        },
        restore: async () => undefined,
        snapshot: async () => ({ cookies: ['old-cookie'] })
      },
      persistRemoval: () => {
        persistCalled = true
        rows.splice(0, 1)
      }
    }),
    /credential cleanup failed/
  )
  assert.equal(clearInjected, true, 'the injected cleanup failure must execute')
  assert.equal(persistCalled, false)
  assert.deepEqual(rows, [row])
})

test('registry-write failure restores credentials and leaves the registry row intact', async () => {
  const primary = new Error('registry write failed')
  const row = { id: 'gateway-a' }
  const rows = [row]
  let restoreExecuted = false

  await assert.rejects(
    removeRegistryConnectionTransactionally({
      credentials: {
        clear: async () => undefined,
        restore: async snapshot => {
          restoreExecuted = true
          assert.deepEqual(snapshot, { cookies: ['old-cookie'] })
        },
        snapshot: async () => ({ cookies: ['old-cookie'] })
      },
      persistRemoval: () => {
        throw primary
      }
    }),
    error => error === primary
  )
  assert.equal(restoreExecuted, true, 'the successful restoration hook must execute')
  assert.deepEqual(rows, [row])
})

test('removal preserves simultaneous registry-write and credential-restore failures as uncertain state', async () => {
  const primary = new Error('registry write failed')
  const rollback = new Error('credential restore failed')
  let writeInjected = false
  let restoreInjected = false

  await assert.rejects(
    removeRegistryConnectionTransactionally({
      credentials: {
        clear: async () => undefined,
        restore: async () => {
          restoreInjected = true
          throw rollback
        },
        snapshot: async () => ({ cookies: ['old-cookie'] })
      },
      persistRemoval: () => {
        writeInjected = true
        throw primary
      }
    }),
    error => {
      assert.ok(error instanceof AggregateError)
      assert.deepEqual(error.errors, [primary, rollback])
      assert.equal((error as AggregateError & { stateUncertain?: boolean }).stateUncertain, true)

      return true
    }
  )
  assert.equal(writeInjected, true, 'the injected registry-write failure must execute')
  assert.equal(restoreInjected, true, 'the injected restoration failure must execute')
})

test('successful removal clears credentials before deleting the registry row', async () => {
  const events: string[] = []

  const result = await removeRegistryConnectionTransactionally({
    credentials: {
      clear: async () => { events.push('clear') },
      restore: async () => { events.push('restore') },
      snapshot: async () => {
        events.push('snapshot')

        return { cookies: ['old-cookie'] }
      }
    },
    persistRemoval: () => {
      events.push('delete-row')

      return 'removed'
    }
  })

  assert.equal(result, 'removed')
  assert.deepEqual(events, ['snapshot', 'clear', 'delete-row'])
})

test('Electron native-token cache changes only after durable persistence succeeds', () => {
  const store = mainSourceSlice('function _storeNativeTokens(', '\nfunction _clearNativeTokens(')
  const clear = mainSourceSlice('function _clearNativeTokens(', '\n// True when we hold native bearer tokens')

  assert.ok(store.indexOf('_persistNativeTokens(') < store.indexOf('_nativeTokens.set('))
  assert.ok(clear.indexOf('_persistNativeTokens(') < clear.indexOf('_nativeTokens.delete('))
})

test('automatic terminal token cleanup explicitly chooses best-effort persistence policy', () => {
  const ensure = mainSourceSlice('async function ensureNativeAccessToken(', '\ntype RegistryNativeTokenSnapshot =')

  assert.match(ensure, /clearNativeTokensBestEffort\(baseUrl, authScope\)/)
  assert.doesNotMatch(ensure, /_clearNativeTokens\(baseUrl, authScope\)/)
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
