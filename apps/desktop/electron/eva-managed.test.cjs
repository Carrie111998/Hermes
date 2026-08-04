const assert = require('node:assert/strict')
const test = require('node:test')
const vm = require('node:vm')
const { version: desktopPackageVersion } = require('../package.json')

const {
  EVA_MANAGED_POLICY,
  EvaBrokerError,
  assertEvaManagedApiRequestAllowed,
  assertEvaManagedLocalMutationAllowed,
  assertEvaManagedLocalTerminalAllowed,
  brokerPost,
  buildEvaAccountRendererResetScript,
  buildEvaDesktopAuthUrl,
  buildEvaManagedWsUrl,
  evaDesktopCodeChallenge,
  isEvaManagedGatewayMethodBlocked,
  isEvaManagedGatewayRequestBlocked,
  launchEvaHermesRuntime,
  makeEvaDesktopCodeVerifier,
  normalizeHermesEnrollment,
  parseEvaDesktopAuthCallback,
  pollEvaDeviceCode,
  publicEvaEnrollmentStatus
} = require('./eva-managed.cjs')

const FUTURE = '2099-07-19T12:00:00.000Z'

test('managed policy is remote-only, account-neutral, and has no Nous endpoint', () => {
  const serialized = JSON.stringify(EVA_MANAGED_POLICY)
  assert.equal(Object.hasOwn(EVA_MANAGED_POLICY, 'customerId'), false)
  assert.equal(EVA_MANAGED_POLICY.runtime, 'hermes')
  assert.equal(EVA_MANAGED_POLICY.updateChannel, 'managed-beta')
  assert.equal(Object.hasOwn(EVA_MANAGED_POLICY, 'allowedAgentIds'), false)
  assert.equal(EVA_MANAGED_POLICY.runtimeHostSuffix, '.ecs.electricsheephq.com')
  assert.doesNotMatch(serialized, /nousresearch|portal\.nous|github\.com/i)
})

test('managed mode refuses to start a local terminal for a remote agent', () => {
  assert.throws(
    () => assertEvaManagedLocalTerminalAllowed(true),
    error =>
      error instanceof EvaBrokerError &&
      error.statusCode === 403 &&
      error.code === 'managed-terminal-unavailable' &&
      /unavailable for this managed remote agent/i.test(error.message)
  )
  assert.doesNotThrow(() => assertEvaManagedLocalTerminalAllowed(false))
})

test('managed mode fails closed before local machine mutation while unmanaged mode preserves it', () => {
  const capabilities = [
    'Writing local files',
    'Renaming local files',
    'Trashing local files',
    'Mutating local Git state',
    'Controlling local terminal processes'
  ]

  for (const capability of capabilities) {
    let managedMutationRan = false
    assert.throws(
      () => {
        assertEvaManagedLocalMutationAllowed(true, capability)
        managedMutationRan = true
      },
      error =>
        error instanceof EvaBrokerError &&
        error.statusCode === 403 &&
        error.code === 'managed-local-mutation-unavailable' &&
        error.message.includes(capability)
    )
    assert.equal(managedMutationRan, false)

    let unmanagedMutationRan = false
    assert.doesNotThrow(() => {
      assertEvaManagedLocalMutationAllowed(false, capability)
      unmanagedMutationRan = true
    })
    assert.equal(unmanagedMutationRan, true)
  }
})

test('managed gateway policy blocks hidden Nous billing methods and their future namespaces', () => {
  for (const method of [
    'billing.state',
    'billing.charge',
    'billing.auto_reload',
    'billing.step_up',
    'subscription.state',
    'subscription.change',
    'subscription.resume',
    'subscription.upgrade',
    'usage.bars',
    'billing.future_method',
    'subscription.future_method'
  ]) {
    assert.equal(isEvaManagedGatewayMethodBlocked(method), true)
  }

  assert.equal(isEvaManagedGatewayMethodBlocked('session.status'), false)
  assert.equal(isEvaManagedGatewayMethodBlocked('usage.snapshot'), false)
  assert.equal(isEvaManagedGatewayMethodBlocked('plugin.billing-helper.run'), false)
})

test('managed gateway policy blocks only hidden commands inside supported generic dispatch envelopes', () => {
  for (const [method, params] of [
    ['slash.exec', { command: '/subscription change' }],
    ['slash.exec', { command: 'TOPUP' }],
    ['command.dispatch', { arg: 'now', name: '/upgrade' }],
    ['cli.exec', { argv: ['subscription'] }],
    ['cli.exec', { argv: ['--profile', 'research', 'topup'] }],
    ['cli.exec', { argv: ['--profile=research', 'upgrade'] }]
  ]) {
    assert.equal(isEvaManagedGatewayRequestBlocked(method, params), true)
  }

  for (const [method, params] of [
    ['slash.exec', { command: 'my-billing-skill report' }],
    ['slash.exec', { command: 'status subscription' }],
    ['command.dispatch', { arg: 'subscription', name: 'status' }],
    ['cli.exec', { argv: ['sessions', 'rename', 'abc', 'subscription'] }],
    ['future.dispatch', { command: 'subscription' }]
  ]) {
    assert.equal(isEvaManagedGatewayRequestBlocked(method, params), false)
  }
})

test('evaOS Agent auth URL carries an S256 challenge and never leaks its verifier', () => {
  const verifier = makeEvaDesktopCodeVerifier({ randomBytes: () => Buffer.alloc(32, 7) })
  const challenge = evaDesktopCodeChallenge(verifier)
  const url = new URL(buildEvaDesktopAuthUrl(challenge, 'state-12345678'))
  assert.match(verifier, /^[A-Za-z0-9_-]{43}$/)
  assert.match(challenge, /^[A-Za-z0-9_-]{43}$/)
  assert.notEqual(challenge, verifier)
  assert.equal(url.origin + url.pathname, EVA_MANAGED_POLICY.dashboardAuthUrl)
  assert.equal(url.searchParams.get('callback_scheme'), 'evaos-agent')
  assert.equal(url.searchParams.get('desktop_auth_state'), 'state-12345678')
  assert.equal(url.searchParams.get('desktop_code_challenge'), challenge)
  assert.equal(url.searchParams.get('desktop_code_challenge_method'), 'S256')
  assert.equal(url.searchParams.get('switch_account'), '1')
  assert.equal(url.searchParams.has('fresh'), false)
  assert.equal(url.searchParams.has('agent_id'), false)
  assert.equal(url.toString().includes(verifier), false)
})

test('broker requests identify the actual Desktop package version', async () => {
  let clientInfo = null
  await brokerPost(
    { action: 'version-probe' },
    {
      policy: {
        brokerUrl: 'https://broker.example.invalid/runtime',
        brokerRequestTimeoutMs: 1_000
      },
      fetchImpl: async (_url, init) => {
        clientInfo = init.headers['X-Client-Info']
        return new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' }
        })
      }
    }
  )

  assert.equal(clientInfo, `evaos-agent/${desktopPackageVersion}`)
})

test('broker request deadline covers a stalled response body and exposes no raw transport detail', async () => {
  let aborted = false
  const rawDetail = 'socket stalled at internal-broker-host'
  const request = brokerPost(
    { action: 'body-stall-probe' },
    {
      policy: {
        brokerUrl: 'https://broker.example.invalid/runtime',
        brokerRequestTimeoutMs: 15
      },
      fetchImpl: async (_url, init) =>
        new Response(
          new ReadableStream({
            start(controller) {
              controller.enqueue(Buffer.from('{"ok":'))
              init.signal.addEventListener(
                'abort',
                () => {
                  aborted = true
                  controller.error(Object.assign(new Error(rawDetail), { name: 'AbortError' }))
                },
                { once: true }
              )
            }
          }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' }
          }
        )
    }
  )

  let rejection = null
  const state = await Promise.race([
    request.then(
      () => 'resolved',
      error => {
        rejection = error
        return 'rejected'
      }
    ),
    new Promise(resolve => setTimeout(() => resolve('still-pending'), 100))
  ])

  assert.equal(state, 'rejected')
  assert.equal(aborted, true)
  assert.ok(rejection instanceof EvaBrokerError)
  assert.equal(rejection.statusCode, 408)
  assert.equal(rejection.code, 'timeout')
  assert.doesNotMatch(rejection.message, new RegExp(rawDetail))
})

test('device-code polling treats an unregistered code as pending and then accepts the opaque session', async () => {
  let requests = 0
  let clock = 1000
  const verifier = 'v'.repeat(43)
  const result = await pollEvaDeviceCode('A'.repeat(12), verifier, {
    now: () => clock,
    pollMs: 5,
    timeoutMs: 30,
    sleep: async delay => {
      clock += delay
    },
    fetchImpl: async (_url, init) => {
      requests += 1
      const body = JSON.parse(init.body)
      assert.deepEqual(body, {
        action: 'claim_desktop_device_code',
        device_code: 'A'.repeat(12),
        device_code_verifier: verifier
      })
      if (requests === 1) {
        return new Response(JSON.stringify({ error: 'Invalid or expired one-time code' }), {
          status: 401,
          headers: { 'Content-Type': 'application/json' }
        })
      }
      return new Response(
        JSON.stringify({
          desktop_session: 'eds_device_session',
          desktop_session_expires_at: FUTURE,
          email: 'employee@example.invalid'
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      )
    }
  })
  assert.equal(requests, 2)
  assert.equal(result.token, 'eds_device_session')
  assert.equal(result.email, 'employee@example.invalid')
})

test('device-code polling fails immediately on a malformed successful claim', async () => {
  let requests = 0
  let sleeps = 0
  await assert.rejects(
    pollEvaDeviceCode('A'.repeat(12), 'v'.repeat(43), {
      now: () => 1000,
      timeoutMs: 30,
      sleep: async () => {
        sleeps += 1
      },
      fetchImpl: async () => {
        requests += 1
        return new Response(JSON.stringify({ desktop_session_expires_at: FUTURE }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' }
        })
      }
    }),
    error => error instanceof EvaBrokerError && error.code === 'invalid-session'
  )
  assert.equal(requests, 1)
  assert.equal(sleeps, 0)
})

test('device-code polling treats malformed or broker-rejected verifiers as terminal', async () => {
  let requests = 0
  let sleeps = 0
  const options = {
    now: () => 1000,
    timeoutMs: 30,
    sleep: async () => {
      sleeps += 1
    },
    fetchImpl: async () => {
      requests += 1
      return new Response(JSON.stringify({ error: 'A valid device code verifier is required' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' }
      })
    }
  }

  await assert.rejects(
    pollEvaDeviceCode('A'.repeat(12), 'too-short', options),
    error => error instanceof EvaBrokerError && error.statusCode === 400 && error.code === 'invalid-verifier'
  )
  assert.equal(requests, 0)

  await assert.rejects(
    pollEvaDeviceCode('A'.repeat(12), 'v'.repeat(43), options),
    error => error instanceof EvaBrokerError && error.statusCode === 400 && error.code === 'broker-rejected'
  )
  assert.equal(requests, 1)
  assert.equal(sleeps, 0)
})

test('runtime launch lets the broker select the account and assigned agent', async () => {
  let observed = null
  const result = await launchEvaHermesRuntime('eds_desktop_session', {
    fetchImpl: async (url, init) => {
      observed = { url, init, body: JSON.parse(init.body) }
      return new Response(
        JSON.stringify({
          schema_version: 'evaos.hermes_desktop_enrollment.v1',
          runtime: 'hermes',
          customer_id: 'jackie-david',
          remote_backend: {
            base_url: 'https://hermes-jackie-david.ecs.electricsheephq.com/',
            session_token: 'opaque-runtime-session',
            expires_at: FUTURE,
            agent_id: 'jane'
          }
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      )
    }
  })

  assert.equal(observed.url, EVA_MANAGED_POLICY.brokerUrl)
  assert.equal(observed.init.headers.Authorization, 'Bearer eds_desktop_session')
  assert.deepEqual(observed.body, {
    action: 'runtime_launch',
    runtime: 'hermes',
    launch_mode: 'dashboard_surface',
    client_surface: 'eva_desktop'
  })
  assert.equal(Object.hasOwn(observed.body, 'customer_id'), false)
  assert.equal(Object.hasOwn(observed.body, 'agent_id'), false)
  assert.equal(result.agentId, 'jane')
  assert.equal(result.baseUrl, 'https://hermes-jackie-david.ecs.electricsheephq.com')
})

test('managed WebSocket transport uses only the ws-proxy Eva session parameter', () => {
  const url = new URL(buildEvaManagedWsUrl('https://hermes-jackie-david.ecs.electricsheephq.com', 'opaque-token'))
  assert.equal(url.protocol, 'wss:')
  assert.equal(url.pathname, '/api/ws')
  assert.equal(url.searchParams.get('eva_session'), 'opaque-token')
  assert.equal(url.searchParams.has('token'), false)
  assert.equal(url.searchParams.has('agent_id'), false)
})

test('managed backend passes existing and unknown future API features through unchanged', () => {
  const requests = [
    { path: '/api/future-feature?mode=alpha', method: 'GET' },
    { path: '/api/future-feature', method: 'POST', body: { future: true } },
    { path: '/api/future-feature', method: 'PUT', body: { future: true } },
    { path: '/api/future-feature', method: 'DELETE' },
    { path: '/api/image/attach-bytes', method: 'POST', body: { bytes: 'opaque' } },
    { path: '/api/config', method: 'PUT', body: { model: 'configured-remotely' } },
    { path: '/api/cron/jobs', method: 'POST', body: { schedule: '0 9 * * *' } },
    { path: '/api/mcp/servers', method: 'PUT', body: { name: 'example' } }
  ]

  for (const request of requests) {
    const allowed = assertEvaManagedApiRequestAllowed(request)
    assert.equal(allowed.method, request.method)
    assert.equal(allowed.path, request.path)
  }
})

test('managed backend leaves file authorization to the assigned OS-isolated Hermes instance', () => {
  assert.deepEqual(
    assertEvaManagedApiRequestAllowed({
      path: '/api/fs/read-text?path=%2Fsrv%2Fevaos%2Fagents%2Fmain%2FWelcome.md'
    }),
    {
      method: 'GET',
      path: '/api/fs/read-text?path=%2Fsrv%2Fevaos%2Fagents%2Fmain%2FWelcome.md',
      pathname: '/api/fs/read-text'
    }
  )
})

test('managed backend blocks connection, assignment, updater, and Nous OAuth escape hatches', () => {
  const denied = [
    { path: '/api/future-feature', method: 'POST', agentId: 'another-agent' },
    { path: '/api/future-feature', method: 'POST', customer_id: 'another-customer' },
    { path: '/api/future-feature', gatewayUrl: 'https://example.invalid' },
    { path: '/api/future-feature', token: 'raw-token' },
    { path: '/api/future-feature?agent_id=another-agent' },
    { path: '/api/future-feature?eva_session=raw-token' },
    { path: '/api/future-feature?gateway_url=https%3A%2F%2Fexample.invalid' },
    { path: '/api/hermes/update', method: 'POST' },
    { path: '/api/hermes/update/', method: 'POST' },
    { path: '/api/hermes/update/check?force=true', method: 'GET' },
    { path: '/api/hermes/update/check/', method: 'GET' },
    { path: '/api/providers/oauth/nous', method: 'DELETE' },
    { path: '/api/providers/oauth/nous/start', method: 'POST' },
    { path: '/api/providers/oauth/nous/submit', method: 'POST' },
    { path: '/api/providers/oauth/nous/poll/session-1', method: 'GET' }
  ]
  for (const request of denied) {
    assert.throws(
      () => assertEvaManagedApiRequestAllowed(request),
      error => error instanceof EvaBrokerError && error.statusCode === 403 && error.code === 'managed-escape'
    )
  }
})

test('managed backend rejects absolute, non-API, and ambiguous request paths', () => {
  for (const path of [
    'https://example.invalid/api/skills',
    '//example.invalid/api/skills',
    '/health',
    '/api/%zz',
    '/api/%252e%252e/admin',
    '/api/future%253fagent_id=another-agent'
  ]) {
    assert.throws(
      () => assertEvaManagedApiRequestAllowed({ path }),
      error => error instanceof EvaBrokerError && error.code === 'managed-policy'
    )
  }
})

test('managed backend validates routing and endpoint profiles independently', () => {
  assert.deepEqual(assertEvaManagedApiRequestAllowed({ path: '/api/skills', profile: 'research' }), {
    method: 'GET',
    path: '/api/skills?profile=research',
    pathname: '/api/skills'
  })
  assert.deepEqual(assertEvaManagedApiRequestAllowed({ path: '/api/skills?profile=all', profile: 'research' }), {
    method: 'GET',
    path: '/api/skills?profile=all',
    pathname: '/api/skills'
  })
  for (const request of [
    { path: '/api/skills?profile=research' },
    { path: '/api/skills?profile=research&profile=all', profile: 'research' },
    { path: '/api/skills?profile=../other-agent', profile: 'research' },
    { path: '/api/skills', profile: '../other-agent' }
  ]) {
    assert.throws(
      () => assertEvaManagedApiRequestAllowed(request),
      error => error instanceof EvaBrokerError && error.code === 'managed-policy'
    )
  }
})

test('account reset clears renderer account state while preserving global preferences', () => {
  const globalLayoutPresets = JSON.stringify({
    focus: {
      name: 'Focus',
      tree: { type: 'group', id: 'main', panes: ['workspace'], active: 'workspace' }
    }
  })
  const values = new Map([
    ['hermes.desktop.lastSessionId.research', 'session-secret'],
    ['hermes.desktop.workspace-cwd.remote.eva-managed%3A%2F%2Fcustomer.default', '/srv/customer'],
    ['hermes:composer-drafts:v3', '{"session-secret":"draft"}'],
    [
      'hermes.desktop.layoutTree.v2',
      JSON.stringify({
        type: 'group',
        id: 'main',
        panes: ['workspace', 'session-tile:account-a', 'route-tile:/skills'],
        active: 'session-tile:account-a'
      })
    ],
    ['hermes.desktop.userPlacedPanes.v1', '["session-tile:account-a","route-tile:/skills"]'],
    ['hermes.desktop.layoutPresets.v2', globalLayoutPresets],
    ['hermes.desktop.layoutPreset.active', 'focus'],
    ['hermes-desktop-theme-v2', 'nord'],
    ['hermes.desktop.keybinds', '{"newChat":"Cmd+N"}']
  ])
  const localStorage = {
    get length() {
      return values.size
    },
    key(index) {
      return [...values.keys()][index] ?? null
    },
    removeItem(key) {
      values.delete(key)
    }
  }

  vm.runInNewContext(buildEvaAccountRendererResetScript(), { localStorage })

  assert.equal(values.has('hermes.desktop.lastSessionId.research'), false)
  assert.equal(values.has('hermes.desktop.workspace-cwd.remote.eva-managed%3A%2F%2Fcustomer.default'), false)
  assert.equal(values.has('hermes:composer-drafts:v3'), false)
  assert.equal(values.has('hermes.desktop.layoutTree.v2'), false)
  assert.equal(values.has('hermes.desktop.userPlacedPanes.v1'), false)
  assert.equal(values.get('hermes.desktop.layoutPresets.v2'), globalLayoutPresets)
  assert.equal(values.get('hermes.desktop.layoutPreset.active'), 'focus')
  assert.equal(values.get('hermes-desktop-theme-v2'), 'nord')
  assert.equal(values.get('hermes.desktop.keybinds'), '{"newChat":"Cmd+N"}')
})

test('managed enrollment accepts server-selected accounts and rejects mismatched or malformed identities', () => {
  const payload = {
    schema_version: 'evaos.hermes_desktop_enrollment.v1',
    runtime: 'hermes',
    customer_id: 'jackie-david',
    remote_backend: {
      base_url: 'https://hermes-jackie-david.ecs.electricsheephq.com',
      session_token: 'opaque-runtime-session',
      expires_at: FUTURE,
      agent_id: 'louis'
    }
  }
  assert.equal(normalizeHermesEnrollment(payload).agentId, 'louis')
  const benjamin = normalizeHermesEnrollment({
    ...payload,
    customer_id: 'benjamin-kennedy',
    remote_backend: {
      ...payload.remote_backend,
      base_url: 'https://hermes-benjamin-kennedy.ecs.electricsheephq.com',
      agent_id: 'benjamin-agent'
    }
  })
  assert.equal(benjamin.customerId, 'benjamin-kennedy')
  assert.equal(benjamin.agentId, 'benjamin-agent')
  assert.throws(
    () => normalizeHermesEnrollment({ ...payload, customer_id: 'another-customer' }),
    error => error instanceof EvaBrokerError && error.code === 'wrong-customer'
  )
  assert.throws(
    () =>
      normalizeHermesEnrollment({
        ...payload,
        remote_backend: { ...payload.remote_backend, agent_id: '../client-selected-agent' }
      }),
    error => error instanceof EvaBrokerError && error.code === 'wrong-agent'
  )
  assert.throws(
    () =>
      normalizeHermesEnrollment({
        ...payload,
        remote_backend: { ...payload.remote_backend, base_url: 'http://127.0.0.1:9211' }
      }),
    error => error instanceof EvaBrokerError && error.code === 'invalid-enrollment'
  )
  for (const baseUrl of [
    'https://other-customer.ecs.electricsheephq.com',
    'https://hermes-jackie-david.ecs.electricsheephq.com/tenant',
    'https://hermes-jackie-david.ecs.electricsheephq.com.evil.invalid'
  ]) {
    assert.throws(
      () =>
        normalizeHermesEnrollment({
          ...payload,
          remote_backend: { ...payload.remote_backend, base_url: baseUrl }
        }),
      error => error instanceof EvaBrokerError && ['invalid-enrollment', 'wrong-customer'].includes(error.code)
    )
  }
})

test('evaOS Agent deep-link callback requires the exact in-flight auth state', () => {
  const raw = `evaos-agent://auth/callback?device_code=${'A'.repeat(8)}` + '&desktop_auth_state=state-12345678'
  assert.equal(parseEvaDesktopAuthCallback(raw, 'state-12345678').deviceCode, 'A'.repeat(8))
  assert.throws(
    () => parseEvaDesktopAuthCallback(raw, 'state-other-123'),
    error => error instanceof EvaBrokerError && error.code === 'state-mismatch'
  )
  assert.throws(
    () =>
      parseEvaDesktopAuthCallback(
        `evaos-agent://auth/callback?device_code=${'A'.repeat(32)}&desktop_session=leaked&desktop_auth_state=state-12345678`,
        'state-12345678'
      ),
    error => error instanceof EvaBrokerError && error.code === 'invalid-callback'
  )
  assert.throws(
    () => parseEvaDesktopAuthCallback(`${raw}&blueprint=unexpected`, 'state-12345678'),
    error => error instanceof EvaBrokerError && error.code === 'invalid-callback'
  )
  for (const deviceCode of ['A'.repeat(7), 'A'.repeat(41)]) {
    assert.throws(
      () =>
        parseEvaDesktopAuthCallback(
          `evaos-agent://auth/callback?device_code=${deviceCode}&desktop_auth_state=state-12345678`,
          'state-12345678'
        ),
      error => error instanceof EvaBrokerError && error.code === 'invalid-callback'
    )
  }
})

test('renderer-facing enrollment status never exposes tokens or backend URLs', () => {
  const status = publicEvaEnrollmentStatus({
    desktop: { token: 'desktop-secret', expiresAt: FUTURE, email: 'employee@example.invalid' },
    runtime: {
      token: 'runtime-secret',
      expiresAt: FUTURE,
      customerId: 'jackie-david',
      agentId: 'jane',
      baseUrl: 'https://secret-endpoint.example'
    }
  })
  const serialized = JSON.stringify(status)
  assert.equal(status.agentId, 'jane')
  assert.doesNotMatch(serialized, /desktop-secret|runtime-secret|secret-endpoint/)
})
