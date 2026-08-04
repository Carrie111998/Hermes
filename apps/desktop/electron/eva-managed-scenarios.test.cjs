const assert = require('node:assert/strict')
const { EventEmitter } = require('node:events')
const fs = require('node:fs')
const net = require('node:net')
const os = require('node:os')
const path = require('node:path')
const test = require('node:test')

const {
  EVA_MANAGED_POLICY,
  EvaBrokerError,
  pollEvaDeviceCode
} = require('./eva-managed.cjs')
const { createEvaAppUpdater } = require('./eva-app-updater.cjs')
const { createEvaManagedRuntime } = require('./eva-runtime.cjs')
const { createEvaWsRelay } = require('./eva-ws-relay.cjs')

const FUTURE = '2099-08-04T12:00:00.000Z'
const MANAGED_BASE_URL = 'https://hermes-customer-one.ecs.electricsheephq.com'

function makeSandbox(t, prefix) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), `${prefix}-`))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  return path.join(directory, 'eva-enrollment.json')
}

function writeEnrollment(statePath, overrides = {}) {
  fs.writeFileSync(
    statePath,
    JSON.stringify({
      schema_version: EVA_MANAGED_POLICY.schemaVersion,
      signed_out: false,
      desktop: {
        token: overrides.desktopToken ?? 'desktop-session',
        expires_at: FUTURE,
        email: 'employee@example.invalid'
      },
      runtime: overrides.runtime === null
        ? null
        : {
            token: overrides.runtimeToken ?? 'runtime-session',
            expires_at: FUTURE,
            base_url: MANAGED_BASE_URL,
            agent_id: 'main',
            customer_id: 'customer-one',
            runtime: 'hermes'
          }
    })
  )
}

function managedRuntime(statePath, overrides = {}) {
  return createEvaManagedRuntime({
    statePath,
    encryptSecret: value => value,
    decryptSecret: value => value,
    waitForHermes: async () => undefined,
    fetchJson: async () => ({ ok: true }),
    createWsRelay: () => ({
      mintTicket: async () => 'ws://127.0.0.1:12345/api/ws?ticket=fixture',
      disconnectAll: () => undefined,
      close: async () => undefined
    }),
    resetRenderer: async () => undefined,
    resolveTimeoutMs: () => 1_000,
    ...overrides
  })
}

function fakeUpstream() {
  const requests = []
  const sockets = new Set()
  const server = net.createServer(socket => {
    sockets.add(socket)
    socket.once('close', () => sockets.delete(socket))
    socket.once('data', chunk => {
      requests.push(chunk.toString('latin1'))
      socket.write(
        'HTTP/1.1 101 Switching Protocols\r\n' +
          'Upgrade: websocket\r\n' +
          'Connection: Upgrade\r\n' +
          'Sec-WebSocket-Accept: fixture\r\n\r\n'
      )
    })
  })

  return {
    connect: () => {
      const address = server.address()
      return new Promise((resolve, reject) => {
        const socket = net.connect(address.port, '127.0.0.1')
        socket.once('connect', () => resolve(socket))
        socket.once('error', reject)
      })
    },
    requests,
    start: () => new Promise(resolve => server.listen(0, '127.0.0.1', resolve)),
    stop: async () => {
      for (const socket of sockets) socket.destroy()
      await new Promise(resolve => server.close(resolve))
    }
  }
}

function upgrade(localUrl) {
  const url = new URL(localUrl)
  return new Promise((resolve, reject) => {
    const socket = net.connect(Number(url.port), url.hostname)
    let response = ''
    socket.once('connect', () => {
      socket.write(
        `GET ${url.pathname}${url.search} HTTP/1.1\r\n` +
          `Host: ${url.host}\r\n` +
          'Connection: Upgrade\r\n' +
          'Upgrade: websocket\r\n' +
          'Sec-WebSocket-Key: Zml4dHVyZS1rZXk=\r\n' +
          'Sec-WebSocket-Version: 13\r\n\r\n'
      )
    })
    socket.on('data', chunk => {
      response += chunk.toString('latin1')
      if (response.includes('\r\n\r\n')) {
        socket.destroy()
        resolve(response)
      }
    })
    socket.once('error', reject)
  })
}

test('managed.signin-assignment-chat', async t => {
  const statePath = makeSandbox(t, 'managed-signin-assignment-chat')
  let clock = 1_000
  let claims = 0
  const desktop = await pollEvaDeviceCode('A'.repeat(32), {
    now: () => clock,
    pollMs: 5,
    timeoutMs: 30,
    sleep: async delay => {
      clock += delay
    },
    fetchImpl: async () => {
      claims += 1
      if (claims === 1) {
        return new Response(JSON.stringify({ error: 'Invalid or expired one-time code' }), {
          status: 401,
          headers: { 'Content-Type': 'application/json' }
        })
      }
      return new Response(
        JSON.stringify({
          desktop_session: 'desktop-session',
          desktop_session_expires_at: FUTURE,
          email: 'employee@example.invalid'
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      )
    }
  })
  writeEnrollment(statePath, { desktopToken: desktop.token, runtime: null })

  const minted = []
  const runtime = managedRuntime(statePath, {
    launchRuntime: async token => {
      assert.equal(token, 'desktop-session')
      return {
        schemaVersion: EVA_MANAGED_POLICY.enrollmentSchemaVersion,
        customerId: 'customer-one',
        runtime: 'hermes',
        agentId: 'assigned-agent',
        baseUrl: MANAGED_BASE_URL,
        token: 'assigned-runtime-session',
        expiresAt: FUTURE
      }
    },
    createWsRelay: () => ({
      mintTicket: async request => {
        minted.push(request)
        return 'ws://127.0.0.1:12345/api/ws?ticket=chat'
      },
      disconnectAll: () => undefined,
      close: async () => undefined
    })
  })
  t.after(async () => runtime.close())

  const connection = await runtime.resolveBackend({ profile: 'main' })
  assert.equal(claims, 2)
  assert.equal(connection.baseUrl, 'eva-managed://customer-one')
  assert.equal(connection.token, '')
  assert.equal(connection.profile, 'main')
  assert.deepEqual(minted.map(({ path, profile }) => ({ path, profile })), [
    { path: '/api/ws', profile: 'main' }
  ])
  const status = runtime.status()
  assert.equal(status.managed, true)
  assert.equal(status.productName, 'evaOS Agent')
  assert.equal(status.desktopSessionActive, true)
  assert.equal(status.runtimeSessionActive, true)
  assert.equal(status.customerId, 'customer-one')
  assert.equal(status.agentId, 'assigned-agent')
  assert.equal(status.updateChannel, 'managed-beta')
  assert.equal(status.email, 'employee@example.invalid')
})

test('managed.openai-reauth-profile', async t => {
  const statePath = makeSandbox(t, 'managed-openai-reauth-profile')
  writeEnrollment(statePath)
  const calls = []
  let launches = 0
  const runtime = managedRuntime(statePath, {
    fetchJson: async (url, token, options) => {
      calls.push({ url, token, options })
      if (calls.length === 1) {
        throw new EvaBrokerError('Runtime session expired.', 401, 'unauthorized')
      }
      return { authorization_url: 'https://auth.example.invalid/openai' }
    },
    launchRuntime: async token => {
      launches += 1
      assert.equal(token, 'desktop-session')
      return {
        schemaVersion: EVA_MANAGED_POLICY.enrollmentSchemaVersion,
        customerId: 'customer-one',
        runtime: 'hermes',
        agentId: 'main',
        baseUrl: MANAGED_BASE_URL,
        token: 'refreshed-runtime-session',
        expiresAt: FUTURE
      }
    }
  })
  t.after(async () => runtime.close())

  const result = await runtime.requestApi({
    path: '/api/providers/oauth/openai-codex/start',
    method: 'POST',
    profile: 'research',
    body: { redirect_uri: 'https://desktop.example.invalid/callback' }
  })

  assert.equal(result.authorization_url, 'https://auth.example.invalid/openai')
  assert.equal(launches, 1)
  assert.equal(calls.length, 2)
  for (const call of calls) {
    const url = new URL(call.url)
    assert.equal(url.pathname, '/api/providers/oauth/openai-codex/start')
    assert.equal(url.searchParams.get('profile'), 'research')
    assert.equal(call.options.method, 'POST')
    assert.deepEqual(call.options.body, {
      redirect_uri: 'https://desktop.example.invalid/callback'
    })
  }
  assert.equal(calls[0].token, 'runtime-session')
  assert.equal(calls[1].token, 'refreshed-runtime-session')
})

test('managed.ws-voice-plugin-profile', async t => {
  const upstream = fakeUpstream()
  await upstream.start()
  const relay = createEvaWsRelay({
    connectUpstream: () => upstream.connect(),
    getUpstream: async () => ({
      baseUrl: MANAGED_BASE_URL,
      generation: 7,
      token: 'runtime-session'
    }),
    getGeneration: () => 7
  })
  t.after(async () => {
    await relay.close()
    await upstream.stop()
  })

  const voice = await relay.mintTicket({
    generation: 7,
    path: '/api/audio/speak-stream',
    profile: 'research'
  })
  const plugin = await relay.mintTicket({
    generation: 7,
    path: '/api/plugins/kanban/events?mode=live',
    profile: 'research'
  })
  assert.match(await upgrade(voice), /^HTTP\/1\.1 101/)
  assert.match(await upgrade(plugin), /^HTTP\/1\.1 101/)
  assert.equal(upstream.requests.length, 2)

  const routed = upstream.requests.map(request => {
    const requestTarget = request.split('\r\n', 1)[0].split(' ')[1]
    return new URL(`https://upstream.invalid${requestTarget}`)
  })
  assert.deepEqual(
    routed.map(url => url.pathname),
    ['/api/audio/speak-stream', '/api/plugins/kanban/events']
  )
  assert.deepEqual(
    routed.map(url => url.searchParams.get('profile')),
    ['research', 'research']
  )
  assert.deepEqual(
    routed.map(url => url.searchParams.get('eva_session')),
    ['runtime-session', 'runtime-session']
  )
  assert.equal(routed[1].searchParams.get('mode'), 'live')
})

test('managed.updater-handoff-preservation', async () => {
  class FakeUpdater extends EventEmitter {
    constructor() {
      super()
      this.downloadCalls = 0
      this.installCalls = []
    }

    setFeedURL() {}

    async checkForUpdates() {
      this.emit('update-available', { version: '1.0.1' })
      return { updateInfo: { version: '1.0.1' } }
    }

    async downloadUpdate() {
      this.downloadCalls += 1
      await new Promise(resolve => setImmediate(resolve))
      this.emit('update-downloaded', { version: '1.0.1' })
      return ['/tmp/evaos-agent.zip']
    }

    quitAndInstall(...args) {
      this.installCalls.push(args)
    }
  }

  const updater = new FakeUpdater()
  const scheduled = []
  const service = createEvaAppUpdater({
    app: { getVersion: () => '1.0.0', isPackaged: true },
    autoUpdater: updater,
    emitProgress: () => undefined,
    isPackaged: true,
    platform: 'darwin',
    schedule: callback => {
      scheduled.push(callback)
      return scheduled.length
    }
  })

  const first = service.apply()
  const second = service.apply()
  const [firstResult, secondResult] = await Promise.all([first, second])
  assert.deepEqual(firstResult, {
    ok: true,
    handedOff: true,
    message: 'Installing evaOS Agent 1.0.1.'
  })
  assert.deepEqual(secondResult, firstResult)
  assert.equal(updater.downloadCalls, 1)
  assert.equal(scheduled.length, 1)
  assert.deepEqual(updater.installCalls, [])

  scheduled[0]()
  assert.deepEqual(updater.installCalls, [[false, true]])
})
