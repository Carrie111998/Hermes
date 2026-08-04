const fs = require('node:fs')
const path = require('node:path')

const {
  EVA_MANAGED_POLICY,
  EvaBrokerError,
  assertEvaManagedApiRequestAllowed,
  buildEvaDesktopAuthUrl,
  evaDesktopCodeChallenge,
  expiresSoon,
  launchEvaHermesRuntime,
  makeAuthState,
  makeEvaDesktopCodeVerifier,
  normalizeDesktopSession,
  normalizeHermesEnrollment,
  parseEvaDesktopAuthCallback,
  pollEvaDeviceCode,
  publicEvaEnrollmentStatus,
  revokeEvaDesktopSession
} = require('./eva-managed.cjs')
const { createEvaWsRelay, normalizeEvaWsEndpoint, normalizeEvaWsProfile } = require('./eva-ws-relay.cjs')

const RUNTIME_ENROLLMENT_RETRY_DELAYS_MS = Object.freeze([2_000, 5_000, 10_000, 20_000, 30_000])

function createEvaManagedRuntime(options) {
  if (
    !options?.statePath ||
    typeof options.encryptSecret !== 'function' ||
    typeof options.decryptSecret !== 'function'
  ) {
    throw new TypeError('evaOS Agent managed runtime requires statePath and secret custody functions.')
  }

  const rememberLog = options.rememberLog ?? (() => undefined)
  const advanceBootProgress = options.advanceBootProgress ?? (async () => undefined)
  const updateBootProgress = options.updateBootProgress ?? (() => undefined)
  const resetConnection = options.resetConnection ?? (() => undefined)
  const resetRenderer = options.resetRenderer ?? (async () => undefined)
  const launchRuntime = options.launchRuntime ?? launchEvaHermesRuntime
  const makeCodeVerifier = options.makeCodeVerifier ?? makeEvaDesktopCodeVerifier
  const codeChallengeFor = options.codeChallengeFor ?? evaDesktopCodeChallenge
  const pollDeviceCode = options.pollDeviceCode ?? pollEvaDeviceCode
  const revokeDesktopSession = options.revokeDesktopSession ?? revokeEvaDesktopSession
  const createWsRelay = options.createWsRelay ?? createEvaWsRelay
  const statePath = options.statePath
  const now = options.now ?? Date.now

  let signInPromise = null
  let runtimeEnrollmentPromise = null
  let runtimeEnrollmentPromiseForced = false
  let runtimeEnrollmentFailure = null
  let pendingAuth = null
  let authGeneration = 0
  let runtimeGeneration = 0
  let runtimeSessionGeneration = 0
  let wsRelay = null

  function emptyState(signedOut = false) {
    return { desktop: null, runtime: null, signedOut }
  }

  function readState() {
    let parsed
    try {
      parsed = JSON.parse(fs.readFileSync(statePath, 'utf8'))
    } catch {
      return emptyState()
    }
    if (!parsed || parsed.schema_version !== EVA_MANAGED_POLICY.schemaVersion) return emptyState()

    let desktop = null
    try {
      desktop = normalizeDesktopSession({
        desktop_session: options.decryptSecret(parsed.desktop?.token),
        desktop_session_expires_at: parsed.desktop?.expires_at,
        email: parsed.desktop?.email
      })
    } catch {
      desktop = null
    }

    let runtime = null
    if (desktop) {
      try {
        runtime = normalizeHermesEnrollment({
          schema_version: EVA_MANAGED_POLICY.enrollmentSchemaVersion,
          runtime: parsed.runtime?.runtime,
          customer_id: parsed.runtime?.customer_id,
          remote_backend: {
            base_url: parsed.runtime?.base_url,
            session_token: options.decryptSecret(parsed.runtime?.token),
            expires_at: parsed.runtime?.expires_at,
            agent_id: parsed.runtime?.agent_id
          }
        })
      } catch {
        runtime = null
      }
    }
    return { desktop, runtime, signedOut: parsed.signed_out === true }
  }

  function atomicWrite(payload) {
    fs.mkdirSync(path.dirname(statePath), { recursive: true, mode: 0o700 })
    try {
      fs.chmodSync(path.dirname(statePath), 0o700)
    } catch {
      // The OS may own the parent directory; the state file remains 0600.
    }
    const tempPath = `${statePath}.tmp`
    fs.writeFileSync(tempPath, JSON.stringify(payload, null, 2), { encoding: 'utf8', mode: 0o600 })
    fs.chmodSync(tempPath, 0o600)
    fs.renameSync(tempPath, statePath)
    fs.chmodSync(statePath, 0o600)
  }

  function writeState(state) {
    if (!state?.desktop) {
      if (!state?.signedOut) {
        fs.rmSync(statePath, { force: true })
        return
      }
      atomicWrite({ schema_version: EVA_MANAGED_POLICY.schemaVersion, signed_out: true })
      return
    }

    atomicWrite({
      schema_version: EVA_MANAGED_POLICY.schemaVersion,
      signed_out: false,
      desktop: {
        token: options.encryptSecret(state.desktop.token),
        expires_at: state.desktop.expiresAt,
        email: state.desktop.email
      },
      runtime: state.runtime
        ? {
            token: options.encryptSecret(state.runtime.token),
            expires_at: state.runtime.expiresAt,
            base_url: state.runtime.baseUrl,
            agent_id: state.runtime.agentId,
            customer_id: state.runtime.customerId,
            runtime: state.runtime.runtime
          }
        : null
    })
  }

  function getWsRelay() {
    if (!wsRelay) {
      wsRelay = createWsRelay({
        getGeneration: () => runtimeSessionGeneration,
        getUpstream: async () => {
          const runtime = await ensureRuntimeEnrollment()
          return {
            baseUrl: runtime.baseUrl,
            generation: runtimeSessionGeneration,
            token: runtime.token
          }
        },
        onAuthRejected: clearRuntimeEnrollment,
        onEvent: event => rememberLog(`[eva-managed] websocket relay ${event}`)
      })
    }
    return wsRelay
  }

  function clearRuntimeEnrollment() {
    runtimeGeneration += 1
    runtimeEnrollmentPromise = null
    runtimeEnrollmentPromiseForced = false
    runtimeSessionGeneration += 1
    const state = readState()
    if (state.desktop) writeState({ desktop: state.desktop, runtime: null })
    resetConnection()
    wsRelay?.disconnectAll()
  }

  function resetRuntimeEnrollmentFailure() {
    runtimeEnrollmentFailure = null
  }

  function recordRuntimeEnrollmentFailure(error) {
    const attempts = (runtimeEnrollmentFailure?.attempts ?? 0) + 1
    const delay =
      RUNTIME_ENROLLMENT_RETRY_DELAYS_MS[Math.min(attempts - 1, RUNTIME_ENROLLMENT_RETRY_DELAYS_MS.length - 1)]
    runtimeEnrollmentFailure = {
      attempts,
      error,
      nextRetryAt: now() + delay
    }
  }

  function invalidateAuthWork() {
    authGeneration += 1
    runtimeGeneration += 1
    runtimeSessionGeneration += 1
    const pending = pendingAuth
    if (pending) {
      pendingAuth = null
      try {
        pending.controller?.abort()
      } catch {
        // The pending request already completed.
      }
      pending.authState = null
      pending.deviceCode = null
      pending.verifier = null
      pending.resolveDeviceCode = null
    }
    signInPromise = null
    runtimeEnrollmentPromise = null
    runtimeEnrollmentPromiseForced = false
    resetRuntimeEnrollmentFailure()
    resetConnection()
    wsRelay?.disconnectAll()
  }

  function assertGeneration(auth, runtime = null) {
    if (auth !== authGeneration || (runtime !== null && runtime !== runtimeGeneration)) {
      throw new EvaBrokerError('evaOS Agent ignored a stale sign-in response.', 409, 'stale-auth')
    }
  }

  async function beginSignIn() {
    if (signInPromise) return signInPromise

    const generation = authGeneration
    const task = (async () => {
      let authState = makeAuthState()
      let verifier = makeCodeVerifier()
      let deviceCode = null
      const codeChallenge = codeChallengeFor(verifier)
      const controller = new AbortController()
      let resolveDeviceCode
      let rejectDeviceCode
      const deviceCodePromise = new Promise((resolve, reject) => {
        resolveDeviceCode = resolve
        rejectDeviceCode = reject
      })
      const attempt = {
        authState,
        controller,
        deviceCode: null,
        generation,
        resolveDeviceCode,
        verifier
      }
      controller.signal.addEventListener(
        'abort',
        () => rejectDeviceCode(new EvaBrokerError('evaOS Agent sign-in was cancelled.', 409, 'stale-auth')),
        { once: true }
      )
      pendingAuth = attempt
      try {
        await advanceBootProgress('eva.sign-in', 'Complete evaOS Agent sign-in in your browser', 14)
        await options.openExternal(buildEvaDesktopAuthUrl(codeChallenge, authState))
        deviceCode = await deviceCodePromise
        const desktop = await pollDeviceCode(deviceCode, verifier, { signal: controller.signal })
        controller.abort()
        assertGeneration(generation)
        writeState({ desktop, runtime: null })
        await advanceBootProgress('eva.authorized', 'Electric Sheep sign-in complete', 22)
        return desktop
      } finally {
        controller.abort()
        if (pendingAuth === attempt) pendingAuth = null
        attempt.authState = null
        attempt.deviceCode = null
        attempt.verifier = null
        attempt.resolveDeviceCode = null
        authState = null
        deviceCode = null
        verifier = null
      }
    })()

    signInPromise = task
    void task
      .finally(() => {
        if (signInPromise === task) signInPromise = null
      })
      .catch(() => undefined)
    return task
  }

  function requireSignIn() {
    resetRuntimeEnrollmentFailure()
    writeState(emptyState(true))
    updateBootProgress(
      {
        phase: 'eva.sign-in-required',
        message: 'Sign in to evaOS Agent from Settings.',
        progress: 8,
        running: false,
        error: null
      },
      { allowDecrease: true }
    )
    throw new EvaBrokerError('Sign in to evaOS Agent from Settings.', 401, 'sign-in-required')
  }

  async function ensureDesktopSession() {
    const state = readState()
    if (state.desktop && !expiresSoon(state.desktop.expiresAt, 0)) return state.desktop
    return requireSignIn()
  }

  function statusCodeOf(error) {
    const statusCode = Number(error?.statusCode)
    if (Number.isInteger(statusCode) && statusCode >= 100 && statusCode <= 599) return statusCode
    if (error?.isReauthRequired === true) return 401
    const match = /^\s*(\d{3})(?::|\b)/.exec(String(error?.message || ''))
    return match ? Number(match[1]) : null
  }

  async function ensureRuntimeEnrollment(input = {}) {
    const force = input.force === true
    if (runtimeEnrollmentPromise) {
      if (!force || runtimeEnrollmentPromiseForced) return runtimeEnrollmentPromise
      runtimeGeneration += 1
      runtimeEnrollmentPromise = null
      runtimeEnrollmentPromiseForced = false
    } else if (force) {
      runtimeGeneration += 1
    }
    const auth = authGeneration
    const runtime = runtimeGeneration
    const current = readState()
    if (!force && current.runtime && !expiresSoon(current.runtime.expiresAt)) return current.runtime
    if (!force && runtimeEnrollmentFailure && now() < runtimeEnrollmentFailure.nextRetryAt) {
      throw runtimeEnrollmentFailure.error
    }

    const task = (async () => {
      try {
        const desktop = await ensureDesktopSession()
        assertGeneration(auth, runtime)
        await advanceBootProgress('eva.enroll', 'Resolving your assigned evaOS agent', 26)
        let enrollment
        try {
          enrollment = await launchRuntime(desktop.token)
        } catch (error) {
          if (!(error instanceof EvaBrokerError) || statusCodeOf(error) !== 401) throw error
          assertGeneration(auth, runtime)
          return requireSignIn()
        }
        assertGeneration(auth, runtime)
        writeState({ desktop, runtime: enrollment })
        runtimeSessionGeneration += 1
        resetConnection()
        wsRelay?.disconnectAll()
        resetRuntimeEnrollmentFailure()
        return enrollment
      } catch (error) {
        if (statusCodeOf(error) !== 401 && error?.code !== 'stale-auth') {
          assertGeneration(auth, runtime)
          recordRuntimeEnrollmentFailure(error)
        }
        throw error
      }
    })()

    runtimeEnrollmentPromise = task
    runtimeEnrollmentPromiseForced = force
    void task
      .finally(() => {
        if (runtimeEnrollmentPromise === task) {
          runtimeEnrollmentPromise = null
          runtimeEnrollmentPromiseForced = false
        }
      })
      .catch(() => undefined)
    return task
  }

  async function resolveBackend(input = {}) {
    const profile = normalizeEvaWsProfile(input.profile)
    let runtime = await ensureRuntimeEnrollment({ force: input.force })
    try {
      await options.waitForHermes(runtime.baseUrl, runtime.token)
    } catch (error) {
      if (statusCodeOf(error) !== 401) throw error
      clearRuntimeEnrollment()
      runtime = await ensureRuntimeEnrollment({ force: true })
      await options.waitForHermes(runtime.baseUrl, runtime.token)
    }
    const connection = {
      authMode: 'token',
      baseUrl: `eva-managed://${runtime.customerId}`,
      mode: 'remote',
      source: 'electric-sheep',
      token: '',
      wsUrl: await getWsRelay().mintTicket({
        generation: runtimeSessionGeneration,
        path: '/api/ws',
        profile
      })
    }
    return profile ? { ...connection, profile } : connection
  }

  async function completeCallback(rawUrl) {
    const pending = pendingAuth
    if (!pending) return false
    const callback = parseEvaDesktopAuthCallback(rawUrl, pending.authState)
    if (pendingAuth !== pending)
      throw new EvaBrokerError('evaOS Agent ignored a stale sign-in callback.', 409, 'stale-auth')
    assertGeneration(pending.generation)
    if (pending.deviceCode && callback.deviceCode !== pending.deviceCode) {
      throw new EvaBrokerError('evaOS Agent sign-in device code did not match.', 400, 'device-code-mismatch')
    }
    pending.deviceCode = callback.deviceCode
    pending.resolveDeviceCode?.(callback.deviceCode)
    options.focusWindow?.()
    return true
  }

  async function signIn() {
    invalidateAuthWork()
    writeState(emptyState())
    const desktop = await beginSignIn()
    const runtime = await ensureRuntimeEnrollment({ force: true })
    resetConnection()
    return publicEvaEnrollmentStatus({ desktop, runtime })
  }

  async function signOut() {
    const state = readState()
    invalidateAuthWork()
    writeState(emptyState(true))
    if (state.desktop) await revokeDesktopSession(state.desktop.token).catch(() => false)
    await resetRenderer()
    return { ok: true }
  }

  async function refresh() {
    const previousRuntime = readState().runtime
    const runtime = await ensureRuntimeEnrollment({ force: true })
    const status = publicEvaEnrollmentStatus({ ...readState(), runtime })
    if (
      !previousRuntime ||
      previousRuntime.customerId !== runtime.customerId ||
      previousRuntime.agentId !== runtime.agentId
    ) {
      await resetRenderer()
    }
    return status
  }

  async function requestApi(request, retry = true) {
    const runtime = await ensureRuntimeEnrollment()
    const allowed = assertEvaManagedApiRequestAllowed(request)
    const timeoutMs = options.resolveTimeoutMs(request?.timeoutMs)
    try {
      return await options.fetchJson(`${runtime.baseUrl}${allowed.path}`, runtime.token, {
        method: allowed.method,
        body: request?.body,
        upload: request?.upload,
        timeoutMs
      })
    } catch (error) {
      if (!retry || statusCodeOf(error) !== 401) throw error
      clearRuntimeEnrollment()
      const refreshed = await ensureRuntimeEnrollment({ force: true })
      const next = assertEvaManagedApiRequestAllowed(request)
      return options.fetchJson(`${refreshed.baseUrl}${next.path}`, refreshed.token, {
        method: next.method,
        body: request?.body,
        upload: request?.upload,
        timeoutMs
      })
    }
  }

  async function requestMedia(request, retry = true) {
    if (typeof options.fetchMedia !== 'function') {
      throw new EvaBrokerError('Managed media streaming is unavailable.', 501, 'managed-media-unavailable')
    }

    const runtime = await ensureRuntimeEnrollment()
    const allowed = assertEvaManagedApiRequestAllowed({
      method: 'GET',
      path: request?.path,
      profile: request?.profile
    })
    if (allowed.pathname !== '/api/files/download') {
      throw new EvaBrokerError('Managed media streaming blocked an unsupported endpoint.', 403, 'managed-policy')
    }

    const response = await options.fetchMedia(`${runtime.baseUrl}${allowed.path}`, runtime.token, request?.headers)
    if (retry && response?.status === 401) {
      clearRuntimeEnrollment()
      const refreshed = await ensureRuntimeEnrollment({ force: true })
      const next = assertEvaManagedApiRequestAllowed({
        method: 'GET',
        path: request?.path,
        profile: request?.profile
      })
      return options.fetchMedia(`${refreshed.baseUrl}${next.path}`, refreshed.token, request?.headers)
    }
    return response
  }

  async function close() {
    invalidateAuthWork()
    const relay = wsRelay
    wsRelay = null
    await relay?.close()
    rememberLog('[eva-managed] local relay closed')
  }

  return {
    close,
    completeCallback,
    freshWsUrl: async (input = {}) => {
      await ensureRuntimeEnrollment()
      const request = typeof input === 'string' ? { profile: input } : input
      return getWsRelay().mintTicket({
        generation: runtimeSessionGeneration,
        path: normalizeEvaWsEndpoint(request.path).path,
        profile: normalizeEvaWsProfile(request.profile)
      })
    },
    requestApi,
    requestMedia,
    resolveBackend,
    signIn: async () => {
      const status = await signIn()
      await resetRenderer()
      return status
    },
    signOut,
    refresh,
    status: () => publicEvaEnrollmentStatus(readState())
  }
}

module.exports = { createEvaManagedRuntime }
