const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const test = require('node:test')

const { EvaBrokerError, brokerPost, evaDesktopCodeChallenge } = require('./eva-managed.cjs')
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
  await runtime.resolveBackend()
  assert.equal(launches, 3)

  clock = 2_000
  await runtime.resolveBackend()
  assert.equal(launches, 3)
  outcome = 'success'
  await runtime.refresh()
  assert.equal(launches, 4)
})

test('forced refresh supersedes an automatic enrollment without reusing its stale promise', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'eva-runtime-force-generation-'))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  const statePath = path.join(directory, 'eva-enrollment.json')
  writeEnrollment(statePath)

  const releases = []
  let launches = 0
  const runtime = makeManagedRuntime(statePath, {
    launchRuntime: async () => {
      launches += 1
      const index = launches
      await new Promise(resolve => releases.push(resolve))
      return {
        schemaVersion: 'evaos.hermes_desktop_enrollment.v1',
        customerId: 'customer-one',
        runtime: 'hermes',
        agentId: 'main',
        baseUrl: 'https://hermes-customer-one.ecs.electricsheephq.com',
        token: index === 1 ? 'stale-runtime-token' : 'forced-runtime-token',
        expiresAt: FUTURE
      }
    }
  })

  const automatic = runtime.resolveBackend()
  await new Promise(resolve => setImmediate(resolve))
  const forced = runtime.refresh()
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(launches, 2)

  releases[1]()
  await forced
  releases[0]()
  await assert.rejects(
    automatic,
    error => error instanceof EvaBrokerError && error.code === 'stale-auth'
  )

  const persisted = JSON.parse(fs.readFileSync(statePath, 'utf8'))
  assert.equal(persisted.runtime.token, 'forced-runtime-token')
})

test('failed forced refresh preserves the last-known-good runtime atomically', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'eva-runtime-atomic-refresh-'))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  const statePath = path.join(directory, 'eva-enrollment.json')
  writeActiveEnrollment(statePath)

  const failure = new EvaBrokerError('Runtime enrollment is temporarily unavailable.', 500, 'vm_lookup_failed')
  const runtime = makeManagedRuntime(statePath, {
    launchRuntime: async () => {
      throw failure
    }
  })

  await assert.rejects(runtime.refresh(), error => error === failure)
  const persisted = JSON.parse(fs.readFileSync(statePath, 'utf8'))
  assert.equal(persisted.runtime.token, 'runtime-token')
  assert.equal(runtime.status().runtimeSessionActive, true)
})

test('refresh preserves renderer state while reconnecting the same customer and agent assignment', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'eva-runtime-refresh-same-assignment-'))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  const statePath = path.join(directory, 'eva-enrollment.json')
  writeActiveEnrollment(statePath)

  let connectionResets = 0
  let rendererResets = 0
  const runtime = makeManagedRuntime(statePath, {
    launchRuntime: async () => ({
      schemaVersion: 'evaos.hermes_desktop_enrollment.v1',
      customerId: 'customer-one',
      runtime: 'hermes',
      agentId: 'main',
      baseUrl: 'https://hermes-customer-one.ecs.electricsheephq.com',
      token: 'refreshed-runtime-token',
      expiresAt: FUTURE
    }),
    resetConnection: () => {
      connectionResets += 1
    },
    resetRenderer: async () => {
      rendererResets += 1
    }
  })

  await runtime.refresh()

  const persisted = JSON.parse(fs.readFileSync(statePath, 'utf8'))
  assert.equal(persisted.runtime.token, 'refreshed-runtime-token')
  assert.equal(connectionResets, 1)
  assert.equal(rendererResets, 0)
})

test('refresh resets renderer state when either assignment identity changes', async t => {
  for (const assignment of [
    { customerId: 'customer-two', agentId: 'main' },
    { customerId: 'customer-one', agentId: 'secondary' }
  ]) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'eva-runtime-refresh-new-assignment-'))
    t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
    const statePath = path.join(directory, 'eva-enrollment.json')
    writeActiveEnrollment(statePath)

    let connectionResets = 0
    let rendererResets = 0
    const runtime = makeManagedRuntime(statePath, {
      launchRuntime: async () => ({
        schemaVersion: 'evaos.hermes_desktop_enrollment.v1',
        customerId: assignment.customerId,
        runtime: 'hermes',
        agentId: assignment.agentId,
        baseUrl: `https://hermes-${assignment.customerId}.ecs.electricsheephq.com`,
        token: 'new-assignment-runtime-token',
        expiresAt: FUTURE
      }),
      resetConnection: () => {
        connectionResets += 1
      },
      resetRenderer: async () => {
        rendererResets += 1
      }
    })

    await runtime.refresh()

    assert.equal(connectionResets, 1)
    assert.equal(rendererResets, 1)
  }
})

test('production reauthentication errors trigger one runtime re-enrollment', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'eva-runtime-production-401-'))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  const statePath = path.join(directory, 'eva-enrollment.json')
  writeActiveEnrollment(statePath)

  let waits = 0
  let launches = 0
  const runtime = makeManagedRuntime(statePath, {
    waitForHermes: async () => {
      waits += 1
      if (waits === 1) {
        throw Object.assign(new Error('The remote session needs authentication.'), { isReauthRequired: true })
      }
    },
    launchRuntime: async () => {
      launches += 1
      return {
        schemaVersion: 'evaos.hermes_desktop_enrollment.v1',
        customerId: 'customer-one',
        runtime: 'hermes',
        agentId: 'main',
        baseUrl: 'https://hermes-customer-one.ecs.electricsheephq.com',
        token: 'refreshed-runtime-token',
        expiresAt: FUTURE
      }
    }
  })

  await runtime.resolveBackend()
  assert.equal(waits, 2)
  assert.equal(launches, 1)
  const persisted = JSON.parse(fs.readFileSync(statePath, 'utf8'))
  assert.equal(persisted.runtime.token, 'refreshed-runtime-token')
})

test('PKCE sign-in keeps one verifier per attempt, rejects wrong callbacks, and clears attempt secrets', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'eva-runtime-pkce-lifecycle-'))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  const statePath = path.join(directory, 'eva-enrollment.json')

  const issuedVerifiers = ['A'.repeat(43), 'B'.repeat(43)]
  const opened = []
  const polls = []
  let releaseFirstPoll
  const firstPollGate = new Promise(resolve => {
    releaseFirstPoll = resolve
  })
  const desktop = {
    token: 'desktop-session-token',
    expiresAt: FUTURE,
    email: 'employee@example.invalid'
  }
  const enrollment = {
    schemaVersion: 'evaos.hermes_desktop_enrollment.v1',
    customerId: 'customer-one',
    runtime: 'hermes',
    agentId: 'main',
    baseUrl: 'https://hermes-customer-one.ecs.electricsheephq.com',
    token: 'runtime-session-token',
    expiresAt: FUTURE
  }
  const runtime = makeManagedRuntime(statePath, {
    makeCodeVerifier: () => issuedVerifiers.shift(),
    codeChallengeFor: evaDesktopCodeChallenge,
    openExternal: async url => {
      opened.push(new URL(url))
    },
    pollDeviceCode: async (deviceCode, verifier, options) => {
      polls.push({ deviceCode, verifier, signal: options.signal })
      if (polls.length === 1) await firstPollGate
      return desktop
    },
    launchRuntime: async () => enrollment,
    revokeDesktopSession: async () => true
  })

  const firstSignIn = runtime.signIn()
  await new Promise(resolve => setImmediate(resolve))
  const firstUrl = opened[0]
  const firstState = firstUrl.searchParams.get('desktop_auth_state')
  const firstVerifier = 'A'.repeat(43)
  assert.equal(firstUrl.searchParams.get('desktop_code_challenge'), evaDesktopCodeChallenge(firstVerifier))
  assert.equal(firstUrl.searchParams.get('desktop_code_challenge_method'), 'S256')
  assert.equal(firstUrl.toString().includes(firstVerifier), false)

  await assert.rejects(
    runtime.completeCallback(
      `evaos-agent://auth/callback?device_code=ABCDEFGH&desktop_auth_state=${firstState}-wrong`
    ),
    error => error instanceof EvaBrokerError && error.code === 'state-mismatch'
  )
  await assert.rejects(
    runtime.completeCallback(
      `evaos-agent://auth/callback?device_code=ABCDEFGH&desktop_auth_state=${firstState}&desktop_session=leaked`
    ),
    error => error instanceof EvaBrokerError && error.code === 'invalid-callback'
  )
  assert.equal(
    await runtime.completeCallback(
      `evaos-agent://auth/callback?device_code=ABCDEFGH&desktop_auth_state=${firstState}`
    ),
    true
  )
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(
    polls.map(({ deviceCode, verifier }) => ({ deviceCode, verifier })),
    [{ deviceCode: 'ABCDEFGH', verifier: firstVerifier }]
  )
  await assert.rejects(
    runtime.completeCallback(
      `evaos-agent://auth/callback?device_code=IJKLMNOP&desktop_auth_state=${firstState}`
    ),
    error => error instanceof EvaBrokerError && error.code === 'device-code-mismatch'
  )

  releaseFirstPoll()
  await firstSignIn
  assert.equal(await runtime.completeCallback(
    `evaos-agent://auth/callback?device_code=ABCDEFGH&desktop_auth_state=${firstState}`
  ), false)
  const persistedAfterSuccess = fs.readFileSync(statePath, 'utf8')
  for (const secret of [firstVerifier, firstState, 'ABCDEFGH']) {
    assert.equal(persistedAfterSuccess.includes(secret), false)
  }

  const secondSignIn = runtime.signIn()
  await new Promise(resolve => setImmediate(resolve))
  const secondUrl = opened[1]
  const secondState = secondUrl.searchParams.get('desktop_auth_state')
  const secondVerifier = 'B'.repeat(43)
  assert.equal(secondUrl.searchParams.get('desktop_code_challenge'), evaDesktopCodeChallenge(secondVerifier))
  assert.notEqual(
    secondUrl.searchParams.get('desktop_code_challenge'),
    firstUrl.searchParams.get('desktop_code_challenge')
  )
  assert.notEqual(secondState, firstState)
  assert.equal(secondUrl.toString().includes(secondVerifier), false)

  await runtime.signOut()
  await assert.rejects(
    secondSignIn,
    error => error instanceof EvaBrokerError && error.code === 'stale-auth'
  )
  assert.equal(
    await runtime.completeCallback(
      `evaos-agent://auth/callback?device_code=QRSTUVWX&desktop_auth_state=${secondState}`
    ),
    false
  )
  const persistedAfterSignOut = fs.readFileSync(statePath, 'utf8')
  for (const secret of [secondVerifier, secondState, 'QRSTUVWX']) {
    assert.equal(persistedAfterSignOut.includes(secret), false)
  }
  assert.equal(issuedVerifiers.length, 0)
})

test('an expired PKCE claim clears its callback state and verifier', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'eva-runtime-pkce-expiry-'))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  const statePath = path.join(directory, 'eva-enrollment.json')
  const verifier = 'C'.repeat(43)
  let opened
  const timeout = new EvaBrokerError('evaOS Agent sign-in timed out.', 408, 'timeout')
  const runtime = makeManagedRuntime(statePath, {
    makeCodeVerifier: () => verifier,
    codeChallengeFor: evaDesktopCodeChallenge,
    openExternal: async url => {
      opened = new URL(url)
    },
    pollDeviceCode: async () => {
      throw timeout
    }
  })

  const signIn = runtime.signIn()
  await new Promise(resolve => setImmediate(resolve))
  const state = opened.searchParams.get('desktop_auth_state')
  await runtime.completeCallback(
    `evaos-agent://auth/callback?device_code=ABCDEFGH&desktop_auth_state=${state}`
  )
  await assert.rejects(signIn, error => error === timeout)
  assert.equal(
    await runtime.completeCallback(
      `evaos-agent://auth/callback?device_code=ABCDEFGH&desktop_auth_state=${state}`
    ),
    false
  )
  assert.equal(fs.existsSync(statePath), false)
  assert.equal(opened.toString().includes(verifier), false)
})

test('managed sign-in times out while waiting for an ignored browser callback and can retry', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'eva-runtime-callback-timeout-'))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  const statePath = path.join(directory, 'eva-enrollment.json')
  let opened = 0
  const runtime = makeManagedRuntime(statePath, {
    loginTimeoutMs: 5,
    openExternal: async () => {
      opened += 1
    }
  })

  await assert.rejects(
    runtime.signIn(),
    error => error instanceof EvaBrokerError && error.statusCode === 408 && error.code === 'timeout'
  )
  await assert.rejects(
    runtime.signIn(),
    error => error instanceof EvaBrokerError && error.statusCode === 408 && error.code === 'timeout'
  )

  assert.equal(opened, 2)
  assert.equal(fs.existsSync(statePath), false)
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

test('closing the runtime drops the cached relay before a later reconnect', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'eva-runtime-relay-reopen-'))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  const statePath = path.join(directory, 'eva-enrollment.json')
  writeActiveEnrollment(statePath)

  let relays = 0
  const closed = []
  const runtime = makeManagedRuntime(statePath, {
    createWsRelay: () => {
      const id = ++relays
      return {
        mintTicket: async () => `ws://127.0.0.1:${12_000 + id}/managed`,
        disconnectAll: () => undefined,
        close: async () => closed.push(id)
      }
    }
  })

  const first = await runtime.resolveBackend()
  await runtime.close()
  const second = await runtime.resolveBackend()

  assert.equal(relays, 2)
  assert.deepEqual(closed, [1])
  assert.notEqual(first.wsUrl, second.wsUrl)
})

test('managed media keeps Range and runtime credentials in the main-process fetch seam', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'eva-runtime-media-'))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  const statePath = path.join(directory, 'eva-enrollment.json')
  writeActiveEnrollment(statePath)

  const calls = []
  const response = { status: 206 }
  const runtime = makeManagedRuntime(statePath, {
    fetchMedia: async (url, token, headers) => {
      calls.push({ headers, token, url })
      return response
    }
  })

  const result = await runtime.requestMedia({
    headers: { range: 'bytes=100-199' },
    path: '/api/files/download?path=%2Fsrv%2Frender.mp4',
    profile: 'research'
  })

  assert.equal(result, response)
  assert.deepEqual(calls, [
    {
      headers: { range: 'bytes=100-199' },
      token: 'runtime-token',
      url: 'https://hermes-customer-one.ecs.electricsheephq.com/api/files/download?path=%2Fsrv%2Frender.mp4&profile=research'
    }
  ])
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
