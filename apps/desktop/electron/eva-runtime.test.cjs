const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const test = require('node:test')

const { EvaBrokerError, brokerPost } = require('./eva-managed.cjs')
const { createEvaManagedRuntime } = require('./eva-runtime.cjs')

const FUTURE = '2099-07-23T12:00:00.000Z'
const EXPIRED = '2020-07-23T12:00:00.000Z'

function writeEnrollment(statePath) {
  fs.writeFileSync(
    statePath,
    JSON.stringify({
      schema_version: 'evaos.eva_desktop_managed.v1',
      signed_out: false,
      desktop: {
        token: 'desktop-token',
        expires_at: FUTURE,
        email: 'employee@example.invalid'
      },
      runtime: {
        token: 'expired-runtime-token',
        expires_at: EXPIRED,
        base_url: 'https://hermes-customer-one.ecs.electricsheephq.com',
        agent_id: 'main',
        customer_id: 'customer-one',
        runtime: 'hermes'
      }
    })
  )
}

function writeActiveEnrollment(statePath) {
  fs.writeFileSync(
    statePath,
    JSON.stringify({
      schema_version: 'evaos.eva_desktop_managed.v1',
      signed_out: false,
      desktop: {
        token: 'desktop-token',
        expires_at: FUTURE,
        email: 'employee@example.invalid'
      },
      runtime: {
        token: 'runtime-token',
        expires_at: FUTURE,
        base_url: 'https://hermes-customer-one.ecs.electricsheephq.com',
        agent_id: 'main',
        customer_id: 'customer-one',
        runtime: 'hermes'
      }
    })
  )
}

function makeManagedRuntime(statePath, overrides = {}) {
  return createEvaManagedRuntime({
    statePath,
    encryptSecret: value => value,
    decryptSecret: value => value,
    waitForHermes: async () => undefined,
    fetchJson: async () => ({ ok: true }),
    createWsRelay: () => ({
      mintTicket: async () => 'ws://127.0.0.1:12345/managed',
      disconnectAll: () => undefined,
      close: async () => undefined
    }),
    resetRenderer: async () => undefined,
    resolveTimeoutMs: () => 1_000,
    ...overrides
  })
}

test('cold launch replaces an expired runtime enrollment before connecting', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'eva-runtime-expiry-'))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  const statePath = path.join(directory, 'eva-enrollment.json')
  writeEnrollment(statePath)

  let launches = 0
  const runtime = createEvaManagedRuntime({
    statePath,
    encryptSecret: value => value,
    decryptSecret: value => value,
    launchRuntime: async token => {
      launches += 1
      assert.equal(token, 'desktop-token')
      return {
        schemaVersion: 'evaos.hermes_desktop_enrollment.v1',
        customerId: 'customer-one',
        runtime: 'hermes',
        agentId: 'main',
        baseUrl: 'https://hermes-customer-one.ecs.electricsheephq.com',
        token: 'fresh-runtime-token',
        expiresAt: FUTURE
      }
    },
    waitForHermes: async () => undefined,
    createWsRelay: () => ({
      mintTicket: async () => 'ws://127.0.0.1:12345/managed',
      disconnectAll: () => undefined,
      close: async () => undefined
    }),
    resolveTimeoutMs: () => 1_000
  })

  const backend = await runtime.resolveBackend()
  const persisted = JSON.parse(fs.readFileSync(statePath, 'utf8'))

  assert.equal(launches, 1)
  assert.equal(backend.mode, 'remote')
  assert.equal(backend.baseUrl, 'eva-managed://customer-one')
  assert.equal(persisted.runtime.token, 'fresh-runtime-token')
  assert.equal(persisted.runtime.expires_at, FUTURE)
})

test('failed runtime enrollment is coalesced and automatic retries wait for the shared cooldown', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'eva-runtime-backoff-'))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  const statePath = path.join(directory, 'eva-enrollment.json')
  writeEnrollment(statePath)

  let clock = 0
  let launches = 0
  const failure = new EvaBrokerError('Runtime enrollment is temporarily unavailable.', 500, 'vm_lookup_failed')
  const runtime = makeManagedRuntime(statePath, {
    now: () => clock,
    launchRuntime: async () => {
      launches += 1
      throw failure
    }
  })

  const first = await Promise.allSettled([
    runtime.resolveBackend(),
    runtime.requestApi({ path: '/api/sessions', method: 'GET' }),
    runtime.freshWsUrl()
  ])
  assert.equal(launches, 1)
  assert.deepEqual(
    first.map(result => result.status),
    ['rejected', 'rejected', 'rejected']
  )

  await assert.rejects(runtime.resolveBackend(), error => error === failure)
  assert.equal(launches, 1)

  clock = 2_000
  await assert.rejects(runtime.resolveBackend(), error => error === failure)
  assert.equal(launches, 2)
})

test('explicit refresh bypasses cooldown once, coalesces callers, and success resets backoff', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'eva-runtime-refresh-backoff-'))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  const statePath = path.join(directory, 'eva-enrollment.json')
  writeEnrollment(statePath)

  let clock = 0
  let launches = 0
  let releaseLaunch
  let outcome = 'fail'
  const failure = new EvaBrokerError('Runtime enrollment is temporarily unavailable.', 500, 'vm_lookup_failed')
  const enrollment = {
    schemaVersion: 'evaos.hermes_desktop_enrollment.v1',
    customerId: 'customer-one',
    runtime: 'hermes',
    agentId: 'main',
    baseUrl: 'https://hermes-customer-one.ecs.electricsheephq.com',
    token: 'fresh-runtime-token',
    expiresAt: FUTURE
  }
  const runtime = makeManagedRuntime(statePath, {
    now: () => clock,
    launchRuntime: async () => {
      launches += 1
      if (outcome === 'wait') {
        await new Promise(resolve => {
          releaseLaunch = resolve
        })
      }
      if (outcome === 'fail') throw failure
      return enrollment
    }
  })

  await assert.rejects(runtime.resolveBackend(), error => error === failure)
  assert.equal(launches, 1)

  outcome = 'wait'
  const refreshOne = runtime.refresh()
  const refreshTwo = runtime.refresh()
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(launches, 2)
  outcome = 'success'
  releaseLaunch()
  await Promise.all([refreshOne, refreshTwo])
  assert.equal(launches, 2)

  outcome = 'fail'
  await assert.rejects(runtime.refresh(), error => error === failure)
  assert.equal(launches, 3)
  await assert.rejects(runtime.resolveBackend(), error => error === failure)
  assert.equal(launches, 3)

  clock = 2_000
  await assert.rejects(runtime.resolveBackend(), error => error === failure)
  assert.equal(launches, 4)
})

test('a stale in-flight launch cannot restore backoff after auth invalidation', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'eva-runtime-stale-backoff-'))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  const statePath = path.join(directory, 'eva-enrollment.json')
  writeEnrollment(statePath)

  let launches = 0
  let rejectLaunch
  let outcome = 'wait'
  const failure = new EvaBrokerError('Runtime enrollment is temporarily unavailable.', 500, 'vm_lookup_failed')
  const enrollment = {
    schemaVersion: 'evaos.hermes_desktop_enrollment.v1',
    customerId: 'customer-one',
    runtime: 'hermes',
    agentId: 'main',
    baseUrl: 'https://hermes-customer-one.ecs.electricsheephq.com',
    token: 'fresh-runtime-token',
    expiresAt: FUTURE
  }
  const runtime = makeManagedRuntime(statePath, {
    now: () => 0,
    launchRuntime: async () => {
      launches += 1
      if (outcome === 'wait') {
        await new Promise((_resolve, reject) => {
          rejectLaunch = reject
        })
      }
      return enrollment
    }
  })

  const staleLaunch = runtime.resolveBackend()
  await new Promise(resolve => setImmediate(resolve))
  await runtime.close()
  outcome = 'success'
  rejectLaunch(failure)

  await assert.rejects(staleLaunch, error => error instanceof EvaBrokerError && error.code === 'stale-auth')
  const backend = await runtime.resolveBackend()
  assert.equal(launches, 2)
  assert.equal(backend.baseUrl, 'eva-managed://customer-one')
})

test('a runtime 401 clears older transient backoff before requiring sign-in', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'eva-runtime-sign-in-backoff-'))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  const statePath = path.join(directory, 'eva-enrollment.json')
  writeEnrollment(statePath)

  let launches = 0
  let outcome = 'fail'
  const failure = new EvaBrokerError('Runtime enrollment is temporarily unavailable.', 500, 'vm_lookup_failed')
  const runtime = makeManagedRuntime(statePath, {
    now: () => 0,
    launchRuntime: async () => {
      launches += 1
      if (outcome === 'unauthorized') {
        throw new EvaBrokerError('Desktop session was revoked.', 401, 'unauthorized')
      }
      throw failure
    }
  })

  await assert.rejects(runtime.resolveBackend(), error => error === failure)
  outcome = 'unauthorized'
  await assert.rejects(runtime.refresh(), error => error instanceof EvaBrokerError && error.code === 'sign-in-required')
  await assert.rejects(
    runtime.resolveBackend(),
    error => error instanceof EvaBrokerError && error.code === 'sign-in-required'
  )
  assert.equal(launches, 2)
})

test('managed runtime forwards unknown APIs, bodies, uploads, and Hermes profiles to the assigned backend', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'eva-runtime-passthrough-'))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  const statePath = path.join(directory, 'eva-enrollment.json')
  writeActiveEnrollment(statePath)

  const calls = []
  const runtime = createEvaManagedRuntime({
    statePath,
    encryptSecret: value => value,
    decryptSecret: value => value,
    fetchJson: async (url, token, options) => {
      calls.push({ url, token, options })
      return { ok: true }
    },
    createWsRelay: () => ({
      mintTicket: async () => 'ws://127.0.0.1:12345/managed',
      disconnectAll: () => undefined,
      close: async () => undefined
    }),
    resolveTimeoutMs: () => 1_000
  })

  const upload = {
    filename: 'future.bin',
    contentType: 'application/octet-stream',
    bytes: Buffer.from([1, 2, 3])
  }
  await runtime.requestApi({
    path: '/api/future-feature?mode=alpha',
    method: 'POST',
    profile: 'research',
    body: { future: true },
    upload
  })

  assert.equal(calls.length, 1)
  assert.equal(
    calls[0].url,
    'https://hermes-customer-one.ecs.electricsheephq.com/api/future-feature?mode=alpha&profile=research'
  )
  assert.equal(calls[0].token, 'runtime-token')
  assert.equal(calls[0].options.method, 'POST')
  assert.deepEqual(calls[0].options.body, { future: true })
  assert.equal(calls[0].options.upload, upload)
})

test('managed connections and endpoint tickets preserve the selected profile and runtime generation', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'eva-runtime-ws-profile-'))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  const statePath = path.join(directory, 'eva-enrollment.json')
  writeActiveEnrollment(statePath)

  let relayOptions
  const minted = []
  const runtime = makeManagedRuntime(statePath, {
    createWsRelay: options => {
      relayOptions = options
      return {
        mintTicket: async request => {
          minted.push(request)
          return `ws://127.0.0.1:12345${request.path}?ticket=fresh`
        },
        disconnectAll: () => undefined,
        close: async () => undefined
      }
    }
  })
  t.after(async () => runtime.close())

  const connection = await runtime.resolveBackend({ profile: 'research' })
  assert.equal(connection.profile, 'research')
  assert.equal(connection.token, '')
  assert.deepEqual(minted[0], {
    generation: 0,
    path: '/api/ws',
    profile: 'research'
  })

  await runtime.freshWsUrl({
    path: '/api/plugins/kanban/events?mode=live',
    profile: 'research'
  })
  assert.deepEqual(minted[1], {
    generation: 0,
    path: '/api/plugins/kanban/events?mode=live',
    profile: 'research'
  })

  const upstream = await relayOptions.getUpstream()
  assert.equal(upstream.baseUrl, 'https://hermes-customer-one.ecs.electricsheephq.com')
  assert.equal(upstream.token, 'runtime-token')
  assert.equal(upstream.generation, minted[1].generation)
  assert.equal(relayOptions.getGeneration(), minted[1].generation)
})

test('broker requests time out instead of leaving managed launch unresolved', async () => {
  const policy = {
    brokerUrl: 'https://broker.example.invalid/runtime',
    brokerRequestTimeoutMs: 5
  }

  await assert.rejects(
    brokerPost(
      { action: 'runtime_launch' },
      {
        policy,
        fetchImpl: (_url, init) =>
          new Promise((_resolve, reject) => {
            init.signal.addEventListener(
              'abort',
              () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })),
              { once: true }
            )
          })
      }
    ),
    error => error instanceof EvaBrokerError && error.statusCode === 408 && error.code === 'timeout'
  )
})
