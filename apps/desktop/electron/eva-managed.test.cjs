const assert = require('node:assert/strict')
const test = require('node:test')

const {
  EVA_MANAGED_POLICY,
  EvaBrokerError,
  assertEvaManagedApiRequestAllowed,
  assertEvaManagedLocalTerminalAllowed,
  buildEvaDesktopAuthUrl,
  buildEvaManagedWsUrl,
  launchEvaHermesRuntime,
  makeDeviceCode,
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

test('evaOS Agent auth URL carries a high-entropy fallback code and state but no agent selector', () => {
  const deviceCode = makeDeviceCode({ randomUUID: () => '12345678-1234-4abc-9def-1234567890ab' })
  const url = new URL(buildEvaDesktopAuthUrl(deviceCode, 'state-12345678'))
  assert.equal(deviceCode, '1234567812344ABC9DEF1234567890AB')
  assert.equal(url.origin + url.pathname, EVA_MANAGED_POLICY.dashboardAuthUrl)
  assert.equal(url.searchParams.get('callback_scheme'), 'evaos-agent')
  assert.equal(url.searchParams.get('fresh'), deviceCode)
  assert.equal(url.searchParams.get('desktop_auth_state'), 'state-12345678')
  assert.equal(url.searchParams.get('switch_account'), '1')
  assert.equal(url.searchParams.has('agent_id'), false)
})

test('device-code polling treats an unregistered code as pending and then accepts the opaque session', async () => {
  let requests = 0
  let clock = 1000
  const result = await pollEvaDeviceCode('A'.repeat(32), {
    now: () => clock,
    pollMs: 5,
    timeoutMs: 30,
    sleep: async delay => {
      clock += delay
    },
    fetchImpl: async (_url, init) => {
      requests += 1
      const body = JSON.parse(init.body)
      assert.deepEqual(body, { action: 'claim_desktop_device_code', device_code: 'A'.repeat(32) })
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
    pollEvaDeviceCode('A'.repeat(32), {
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

test('managed backend supports Hermes profiles within one assigned backend', () => {
  assert.deepEqual(assertEvaManagedApiRequestAllowed({ path: '/api/skills', profile: 'research' }), {
    method: 'GET',
    path: '/api/skills?profile=research',
    pathname: '/api/skills'
  })
  assert.deepEqual(assertEvaManagedApiRequestAllowed({ path: '/api/skills?profile=research', profile: 'research' }), {
    method: 'GET',
    path: '/api/skills?profile=research',
    pathname: '/api/skills'
  })
  assert.throws(
    () => assertEvaManagedApiRequestAllowed({ path: '/api/skills?profile=default', profile: 'research' }),
    error => error instanceof EvaBrokerError && error.code === 'managed-policy'
  )
  assert.throws(
    () => assertEvaManagedApiRequestAllowed({ path: '/api/skills', profile: '../other-agent' }),
    error => error instanceof EvaBrokerError && error.code === 'managed-policy'
  )
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
  const raw = `evaos-agent://auth/callback?device_code=${'A'.repeat(32)}` + '&desktop_auth_state=state-12345678'
  assert.equal(parseEvaDesktopAuthCallback(raw, 'state-12345678').deviceCode, 'A'.repeat(32))
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
