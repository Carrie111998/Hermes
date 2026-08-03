const crypto = require('node:crypto')

const EVA_MANAGED_POLICY = Object.freeze({
  schemaVersion: 'evaos.eva_desktop_managed.v1',
  enrollmentSchemaVersion: 'evaos.hermes_desktop_enrollment.v1',
  productName: 'evaOS Agent',
  dashboardAuthUrl: 'https://www.electricsheephq.com/desktop-auth',
  brokerUrl: 'https://rhfojelkgtwcxnrfhtlj.supabase.co/functions/v1/desktop-runtime-session',
  runtime: 'hermes',
  launchMode: 'dashboard_surface',
  clientSurface: 'eva_desktop',
  callbackScheme: 'evaos-agent',
  updateChannel: 'managed-beta',
  deviceCodePollMs: 1_500,
  loginTimeoutMs: 180_000,
  brokerRequestTimeoutMs: 15_000,
  runtimeRefreshSkewMs: 60_000,
  runtimeHostSuffix: '.ecs.electricsheephq.com'
})

const EVA_MANAGED_API_METHODS = new Set(['DELETE', 'GET', 'HEAD', 'OPTIONS', 'PATCH', 'POST', 'PUT'])
const EVA_MANAGED_ESCAPE_FIELDS = new Set([
  'agentId',
  'agent_id',
  'baseUrl',
  'base_url',
  'customerId',
  'customer_id',
  'gatewayUrl',
  'gateway_url',
  'sessionToken',
  'session_token',
  'token',
  'url'
])
const EVA_MANAGED_ESCAPE_QUERY_KEYS = new Set([
  'agent',
  'agent_id',
  'base_url',
  'customer',
  'customer_id',
  'eva_session',
  'gateway',
  'gateway_url',
  'session_token',
  'token'
])
const EVA_MANAGED_BLOCKED_BACKEND_PATHS = new Set(['/api/hermes/update', '/api/hermes/update/check'])
const EVA_MANAGED_BLOCKED_BACKEND_PREFIXES = ['/api/providers/oauth/nous']
const EVA_MANAGED_PROFILE_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/

class EvaBrokerError extends Error {
  constructor(message, statusCode = null, code = 'broker-error') {
    super(message)
    this.name = 'EvaBrokerError'
    this.statusCode = statusCode
    this.code = code
  }
}

function normalizeEvaManagedApiPath(value) {
  const rawPath = String(value || '')
  if (!rawPath.startsWith('/') || rawPath.startsWith('//') || rawPath.includes('\\')) {
    throw new EvaBrokerError('evaOS Agent blocked an invalid managed-backend request.', 400, 'managed-policy')
  }

  let parsed
  try {
    parsed = new URL(rawPath, 'https://eva-managed.invalid')
  } catch {
    throw new EvaBrokerError('evaOS Agent blocked an invalid managed-backend request.', 400, 'managed-policy')
  }
  if (parsed.origin !== 'https://eva-managed.invalid' || !parsed.pathname.startsWith('/api/')) {
    throw new EvaBrokerError('evaOS Agent blocked a request outside the managed API.', 403, 'managed-policy')
  }

  let pathname = parsed.pathname
  for (let pass = 0; pass < 3; pass += 1) {
    let decoded
    try {
      decoded = decodeURIComponent(pathname)
    } catch {
      throw new EvaBrokerError('evaOS Agent blocked an invalid managed-backend request.', 400, 'managed-policy')
    }
    if (decoded === pathname) break
    pathname = decoded
  }
  pathname = pathname.replace(/\/+/g, '/')
  const segments = pathname.split('/')
  if (
    pathname.includes('%') ||
    pathname.includes('\\') ||
    pathname.includes('?') ||
    pathname.includes('#') ||
    hasAsciiControl(pathname) ||
    segments.some(segment => segment === '.' || segment === '..') ||
    !pathname.startsWith('/api/')
  ) {
    throw new EvaBrokerError('evaOS Agent blocked an ambiguous managed-backend request.', 400, 'managed-policy')
  }
  return { parsed, pathname }
}

function hasAsciiControl(value) {
  return Array.from(value).some(character => {
    const codePoint = character.codePointAt(0)
    return codePoint === undefined || codePoint <= 0x1f || codePoint === 0x7f
  })
}

function assertEvaManagedApiRequestAllowed(request) {
  const method = String(request?.method || 'GET').toUpperCase()
  if (!EVA_MANAGED_API_METHODS.has(method)) {
    throw new EvaBrokerError('evaOS Agent blocked an invalid managed-backend method.', 400, 'managed-policy')
  }
  for (const key of EVA_MANAGED_ESCAPE_FIELDS) {
    if (Object.hasOwn(request ?? {}, key)) {
      throw new EvaBrokerError(
        'evaOS Agent connection and assignment are managed by Electric Sheep.',
        403,
        'managed-escape'
      )
    }
  }

  const { parsed, pathname } = normalizeEvaManagedApiPath(request?.path)
  const policyPath = pathname.length > '/api/'.length ? pathname.replace(/\/+$/, '') : pathname
  if (
    EVA_MANAGED_BLOCKED_BACKEND_PATHS.has(policyPath) ||
    EVA_MANAGED_BLOCKED_BACKEND_PREFIXES.some(
      prefix => policyPath === prefix || policyPath.startsWith(`${prefix}/`)
    )
  ) {
    throw new EvaBrokerError('This action is unavailable for managed evaOS Agent accounts.', 403, 'managed-escape')
  }

  for (const [key, value] of parsed.searchParams.entries()) {
    if (EVA_MANAGED_ESCAPE_QUERY_KEYS.has(key)) {
      throw new EvaBrokerError(
        'evaOS Agent connection and assignment are managed by Electric Sheep.',
        403,
        'managed-escape'
      )
    }
    if (hasAsciiControl(key) || hasAsciiControl(value)) {
      throw new EvaBrokerError('evaOS Agent blocked an invalid managed query.', 400, 'managed-policy')
    }
  }

  const profile = request?.profile == null ? null : String(request.profile)
  if (profile && !EVA_MANAGED_PROFILE_RE.test(profile)) {
    throw new EvaBrokerError('evaOS Agent blocked an invalid Hermes profile.', 400, 'managed-policy')
  }
  if (profile && parsed.searchParams.has('profile') && parsed.searchParams.get('profile') !== profile) {
    throw new EvaBrokerError('evaOS Agent blocked conflicting Hermes profiles.', 400, 'managed-policy')
  }
  if (profile && !parsed.searchParams.has('profile')) {
    parsed.searchParams.set('profile', profile)
  }

  const query = parsed.searchParams.toString()
  return { method, pathname, path: `${pathname}${query ? `?${query}` : ''}` }
}

function assertEvaManagedLocalTerminalAllowed(managed) {
  if (managed) {
    throw new EvaBrokerError(
      'Terminal access is unavailable for this managed remote agent.',
      403,
      'managed-terminal-unavailable'
    )
  }
}

function makeDeviceCode(cryptoApi = crypto) {
  const value = String(cryptoApi.randomUUID())
    .toUpperCase()
    .replace(/[^A-Z0-9]/g, '')
  if (value.length < 24 || value.length > 40) {
    throw new Error('Could not create a valid evaOS Agent device code.')
  }
  return value
}

function makeAuthState(cryptoApi = crypto) {
  return String(cryptoApi.randomUUID())
}

function buildEvaDesktopAuthUrl(deviceCode, authState, policy = EVA_MANAGED_POLICY) {
  const normalizedCode = String(deviceCode || '')
    .toUpperCase()
    .replace(/[^A-Z0-9]/g, '')
  if (normalizedCode.length < 24 || normalizedCode.length > 40) {
    throw new Error('evaOS Agent device code must contain 24 to 40 letters or digits.')
  }
  if (!/^[A-Za-z0-9._:-]{8,128}$/.test(String(authState || ''))) {
    throw new Error('evaOS Agent desktop auth state is invalid.')
  }

  const url = new URL(policy.dashboardAuthUrl)
  url.searchParams.set('desktop_app', '1')
  url.searchParams.set('fresh', normalizedCode)
  url.searchParams.set('callback_scheme', policy.callbackScheme)
  url.searchParams.set('desktop_auth_state', String(authState))
  url.searchParams.set('switch_account', '1')
  url.searchParams.set('prompt', 'select_account')
  return url.toString()
}

function parseFutureTimestamp(value, label, now = Date.now()) {
  const timestamp = Date.parse(String(value || ''))
  if (!Number.isFinite(timestamp) || timestamp <= now) {
    throw new EvaBrokerError(`${label} is missing or expired.`, 401, 'session-expired')
  }
  return new Date(timestamp).toISOString()
}

function normalizeOpaqueToken(value, label) {
  const token = String(value || '').trim()
  const hasWhitespaceOrControl = Array.from(token).some(character => {
    const codePoint = character.codePointAt(0)
    return character.trim().length === 0 || codePoint === undefined || codePoint <= 0x1f
  })
  if (!token || token.length > 4096 || hasWhitespaceOrControl) {
    throw new EvaBrokerError(`${label} is invalid.`, 401, 'invalid-session')
  }
  return token
}

function normalizeDesktopSession(payload, now = Date.now()) {
  if (!payload || typeof payload !== 'object') {
    throw new EvaBrokerError('Electric Sheep sign-in returned an invalid session.', 401, 'invalid-session')
  }
  return {
    token: normalizeOpaqueToken(payload.desktop_session, 'Electric Sheep desktop session'),
    expiresAt: parseFutureTimestamp(
      payload.desktop_session_expires_at ?? payload.expires_at,
      'Electric Sheep desktop session',
      now
    ),
    email: typeof payload.email === 'string' && payload.email.trim() ? payload.email.trim().slice(0, 320) : null
  }
}

function parseEvaDesktopAuthCallback(rawUrl, expectedState) {
  let url
  try {
    url = new URL(String(rawUrl || ''))
  } catch {
    throw new EvaBrokerError('evaOS Agent received an invalid sign-in callback.', 400, 'invalid-callback')
  }
  if (
    url.protocol !== `${EVA_MANAGED_POLICY.callbackScheme}:` ||
    url.hostname !== 'auth' ||
    url.pathname !== '/callback'
  ) {
    throw new EvaBrokerError('evaOS Agent received an unexpected sign-in callback.', 400, 'invalid-callback')
  }
  const callbackKeys = [...url.searchParams.keys()]
  if (
    callbackKeys.length !== 2 ||
    new Set(callbackKeys).size !== 2 ||
    !callbackKeys.includes('desktop_auth_state') ||
    !callbackKeys.includes('device_code')
  ) {
    throw new EvaBrokerError('evaOS Agent received an unexpected sign-in callback shape.', 400, 'invalid-callback')
  }
  if (!expectedState || url.searchParams.get('desktop_auth_state') !== expectedState) {
    throw new EvaBrokerError('evaOS Agent sign-in state did not match.', 400, 'state-mismatch')
  }
  if (url.searchParams.has('desktop_session') || url.searchParams.has('session_token')) {
    throw new EvaBrokerError('evaOS Agent sign-in callbacks may not carry session tokens.', 400, 'token-in-callback')
  }
  const deviceCode = String(url.searchParams.get('device_code') || '')
    .toUpperCase()
    .replace(/[^A-Z0-9]/g, '')
  if (deviceCode.length < 24 || deviceCode.length > 40) {
    throw new EvaBrokerError(
      'evaOS Agent sign-in callback did not contain a valid device code.',
      400,
      'invalid-callback'
    )
  }
  return { deviceCode }
}

function normalizeRemoteBaseUrl(value, policy = EVA_MANAGED_POLICY) {
  let url
  try {
    url = new URL(String(value || ''))
  } catch {
    throw new EvaBrokerError('Electric Sheep returned an invalid managed backend.', 502, 'invalid-enrollment')
  }
  const hostname = url.hostname.toLowerCase()
  if (
    url.protocol !== 'https:' ||
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    url.pathname !== '/' ||
    !hostname.endsWith(policy.runtimeHostSuffix) ||
    !/^hermes-[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?$/.test(
      hostname.slice(0, -policy.runtimeHostSuffix.length)
    )
  ) {
    throw new EvaBrokerError('Electric Sheep returned an untrusted managed backend.', 502, 'invalid-enrollment')
  }
  return url.origin
}

function buildEvaManagedWsUrl(baseUrl, token) {
  const parsed = new URL(normalizeRemoteBaseUrl(baseUrl))
  const scheme = parsed.protocol === 'https:' ? 'wss' : 'ws'
  const prefix = parsed.pathname.replace(/\/+$/, '')
  return `${scheme}://${parsed.host}${prefix}/api/ws?eva_session=${encodeURIComponent(
    normalizeOpaqueToken(token, 'evaOS Agent runtime session')
  )}`
}

function normalizeHermesEnrollment(payload, options = {}) {
  const policy = options.policy ?? EVA_MANAGED_POLICY
  const now = options.now ?? Date.now()
  if (!payload || typeof payload !== 'object' || payload.schema_version !== policy.enrollmentSchemaVersion) {
    throw new EvaBrokerError(
      'Electric Sheep returned an unsupported evaOS Agent enrollment.',
      502,
      'invalid-enrollment'
    )
  }
  const customerId = String(payload.customer_id || '').trim().toLowerCase()
  if (payload.runtime !== policy.runtime || !/^[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?$/.test(customerId)) {
    throw new EvaBrokerError('Electric Sheep returned an invalid managed account.', 403, 'wrong-customer')
  }

  const remote = payload.remote_backend
  if (!remote || typeof remote !== 'object') {
    throw new EvaBrokerError('Electric Sheep returned an incomplete evaOS Agent enrollment.', 502, 'invalid-enrollment')
  }
  const agentId = String(remote.agent_id || '').trim()
  if (!/^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/.test(agentId)) {
    throw new EvaBrokerError('Electric Sheep returned an invalid assigned agent.', 403, 'wrong-agent')
  }

  const baseUrl = normalizeRemoteBaseUrl(remote.base_url, policy)
  const expectedOrigin = `https://hermes-${customerId}${policy.runtimeHostSuffix}`
  if (baseUrl !== expectedOrigin) {
    throw new EvaBrokerError('Electric Sheep returned a backend for a different managed account.', 403, 'wrong-customer')
  }

  return {
    schemaVersion: policy.enrollmentSchemaVersion,
    customerId,
    runtime: policy.runtime,
    agentId,
    baseUrl,
    token: normalizeOpaqueToken(remote.session_token, 'evaOS Agent runtime session'),
    expiresAt: parseFutureTimestamp(remote.expires_at, 'evaOS Agent runtime session', now)
  }
}

async function brokerPost(body, options = {}) {
  const policy = options.policy ?? EVA_MANAGED_POLICY
  const fetchImpl = options.fetchImpl ?? globalThis.fetch
  if (typeof fetchImpl !== 'function') {
    throw new EvaBrokerError(
      'evaOS Agent cannot reach Electric Sheep from this runtime.',
      null,
      'transport-unavailable'
    )
  }

  let response
  const requestController = new AbortController()
  const externalSignal = options.signal
  const abortFromExternal = () => requestController.abort(externalSignal?.reason)
  const timeoutMs = options.timeoutMs ?? policy.brokerRequestTimeoutMs
  const timeout = setTimeout(() => requestController.abort(), timeoutMs)
  if (externalSignal?.aborted) {
    abortFromExternal()
  } else {
    externalSignal?.addEventListener('abort', abortFromExternal, { once: true })
  }
  try {
    response = await fetchImpl(policy.brokerUrl, {
      method: 'POST',
      redirect: 'error',
      cache: 'no-store',
      signal: requestController.signal,
      headers: {
        'Content-Type': 'application/json',
        'X-Client-Info': 'evaos-agent/2026.7.20-es.6',
        ...(options.desktopSession ? { Authorization: `Bearer ${options.desktopSession}` } : {})
      },
      body: JSON.stringify(body)
    })
  } catch (error) {
    if (error?.name === 'AbortError') {
      throw new EvaBrokerError('evaOS Agent sign-in timed out.', 408, 'timeout')
    }
    throw new EvaBrokerError('evaOS Agent could not reach Electric Sheep.', null, 'transport-error')
  } finally {
    clearTimeout(timeout)
    externalSignal?.removeEventListener('abort', abortFromExternal)
  }

  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    const safeMessage =
      payload && typeof payload.error === 'string' && payload.error.length <= 240
        ? payload.error
        : `Electric Sheep request failed (${response.status}).`
    throw new EvaBrokerError(safeMessage, response.status, 'broker-rejected')
  }
  return payload
}

async function claimEvaDeviceCode(deviceCode, options = {}) {
  return normalizeDesktopSession(
    await brokerPost(
      {
        action: 'claim_desktop_device_code',
        device_code: String(deviceCode || '')
          .toUpperCase()
          .replace(/[^A-Z0-9]/g, '')
      },
      options
    ),
    options.now ?? Date.now()
  )
}

async function pollEvaDeviceCode(deviceCode, options = {}) {
  const now = options.now ?? (() => Date.now())
  const sleep =
    options.sleep ??
    (delayMs =>
      new Promise(resolve => {
        setTimeout(resolve, delayMs)
      }))
  const policy = options.policy ?? EVA_MANAGED_POLICY
  const deadline = now() + (options.timeoutMs ?? policy.loginTimeoutMs)

  while (now() < deadline) {
    try {
      return await claimEvaDeviceCode(deviceCode, { ...options, now: now() })
    } catch (error) {
      if (!(error instanceof EvaBrokerError) || error.statusCode !== 401 || error.code !== 'broker-rejected') {
        throw error
      }
    }
    await sleep(options.pollMs ?? policy.deviceCodePollMs)
  }
  throw new EvaBrokerError('evaOS Agent sign-in timed out.', 408, 'timeout')
}

async function launchEvaHermesRuntime(desktopSession, options = {}) {
  const policy = options.policy ?? EVA_MANAGED_POLICY
  const payload = await brokerPost(
    {
      action: 'runtime_launch',
      runtime: policy.runtime,
      launch_mode: policy.launchMode,
      client_surface: policy.clientSurface
    },
    { ...options, desktopSession: normalizeOpaqueToken(desktopSession, 'Electric Sheep desktop session') }
  )
  return normalizeHermesEnrollment(payload, options)
}

async function revokeEvaDesktopSession(desktopSession, options = {}) {
  try {
    await brokerPost(
      { action: 'revoke_desktop_session' },
      { ...options, desktopSession: normalizeOpaqueToken(desktopSession, 'Electric Sheep desktop session') }
    )
    return true
  } catch {
    return false
  }
}

function expiresSoon(expiresAt, skewMs = EVA_MANAGED_POLICY.runtimeRefreshSkewMs, now = Date.now()) {
  const timestamp = Date.parse(String(expiresAt || ''))
  return !Number.isFinite(timestamp) || timestamp <= now + skewMs
}

function publicEvaEnrollmentStatus(state, now = Date.now()) {
  const desktop = state?.desktop ?? null
  const runtime = state?.runtime ?? null
  return {
    managed: true,
    productName: EVA_MANAGED_POLICY.productName,
    signedOut: state?.signedOut === true,
    customerId: runtime?.customerId ?? null,
    email: desktop?.email ?? null,
    desktopSessionExpiresAt: desktop?.expiresAt ?? null,
    desktopSessionActive: Boolean(desktop && !expiresSoon(desktop.expiresAt, 0, now)),
    runtimeSessionExpiresAt: runtime?.expiresAt ?? null,
    runtimeSessionActive: Boolean(runtime && !expiresSoon(runtime.expiresAt, 0, now)),
    agentId: runtime?.agentId ?? null,
    updateChannel: EVA_MANAGED_POLICY.updateChannel
  }
}

module.exports = {
  EVA_MANAGED_POLICY,
  EvaBrokerError,
  assertEvaManagedApiRequestAllowed,
  assertEvaManagedLocalTerminalAllowed,
  brokerPost,
  buildEvaDesktopAuthUrl,
  buildEvaManagedWsUrl,
  claimEvaDeviceCode,
  expiresSoon,
  launchEvaHermesRuntime,
  makeAuthState,
  makeDeviceCode,
  normalizeDesktopSession,
  normalizeHermesEnrollment,
  parseEvaDesktopAuthCallback,
  pollEvaDeviceCode,
  publicEvaEnrollmentStatus,
  revokeEvaDesktopSession
}
