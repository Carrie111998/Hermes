export const DEFAULT_BACKEND_READY_TIMEOUT_MS = 45_000
export const DEFAULT_BACKEND_READY_POLL_MS = 500
// A cold backend can stall its event loop for tens of seconds while Windows
// scans and byte-compiles the gateway import tree. At the default 15s socket
// timeout only three probes fit in the budget; a short one keeps retrying
// across the stall. Health only — the legacy /api/status fallback is genuinely
// slow to answer and keeps the caller's default timeout.
export const DEFAULT_HEALTH_PROBE_TIMEOUT_MS = 5_000

type FetchPublicJson = (url: string, options?: { timeoutMs?: number }) => Promise<unknown>
type FetchJson = (url: string, token?: string | null, options?: { timeoutMs?: number }) => Promise<unknown>

// User-facing readiness-failure copy for an expired remote session. Phrased so
// the renderer's isRemoteReauthError() classifies it (substring "remote gateway
// session has expired") and surfaces the "Sign in" recovery instead of the
// local Retry/Repair buttons.
export const REMOTE_SESSION_EXPIRED_MESSAGE =
  'Your remote gateway session has expired. Open Settings → Gateway and sign in again.'

export interface HermesReadyOptions {
  fetchPublicJson: FetchPublicJson
  fetchJson: FetchJson
  // Health-endpoint probe. Defaults to the credential-free fetchPublicJson;
  // oauth/token remotes pass a credentialed probe so an auth-gated /api/health
  // does not 401-loop the whole boot (see resolveReadinessProbeAuth).
  probeHealth?: FetchPublicJson
  token?: string | null
  signal?: AbortSignal
  timeoutMs?: number
  pollMs?: number
  healthProbeTimeoutMs?: number
  sleep?: (ms: number) => Promise<void>
  now?: () => number
}

export function isMissingHealthEndpointError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error ?? '')

  return /^404:/.test(message) || message.includes('endpoint is likely missing')
}

// A confirmed 401/403 from the health probe — the session is rejected, not the
// backend warming up. fetchJson/fetchJsonViaOauthSession surface HTTP errors as
// `${status}: ${body}`, so the status leads the message.
export function isAuthRejectionError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error ?? '')

  return /^(401|403):/.test(message)
}

// True for an error tagged "needs OAuth sign-in" (duck-typed on needsOauthLogin,
// matching @hermes/shared's isGatewayReauthRequired without importing it into
// the main-process bundle). Such a probe failure is terminal for boot — polling
// cannot fix an expired session.
export function isReauthRequiredError(error: unknown): boolean {
  return typeof error === 'object' && error !== null && (error as { needsOauthLogin?: unknown }).needsOauthLogin === true
}

// Build the terminal reauth error a credentialed probe throws on a confirmed
// 401/403. Tagged so the boot latch (isReauthRequiredError) and the renderer
// overlay (message) both recognize it as "sign in again", not a transient blip.
export function makeReauthRequiredError(cause?: unknown): Error {
  const error = new Error(REMOTE_SESSION_EXPIRED_MESSAGE) as Error & { needsOauthLogin: boolean; kind: string }
  error.needsOauthLogin = true
  error.kind = 'reauth'

  if (cause !== undefined) {
    ;(error as Error & { cause?: unknown }).cause = cause
  }

  return error
}

function supersededError() {
  const error: any = new Error('SSH bootstrap was superseded by newer connection settings.')
  error.kind = 'superseded'

  return error
}

export async function waitForHermesReady(baseUrl: string, options: HermesReadyOptions): Promise<void> {
  const timeoutMs = options.timeoutMs ?? DEFAULT_BACKEND_READY_TIMEOUT_MS
  const pollMs = options.pollMs ?? DEFAULT_BACKEND_READY_POLL_MS
  const healthProbeTimeoutMs = options.healthProbeTimeoutMs ?? DEFAULT_HEALTH_PROBE_TIMEOUT_MS
  const now = options.now ?? Date.now
  const signal = options.signal

  const sleep =
    options.sleep ??
    (ms =>
      new Promise<void>((resolve, reject) => {
        const timer = setTimeout(resolve, ms)
        signal?.addEventListener(
          'abort',
          () => {
            clearTimeout(timer)
            reject(supersededError())
          },
          { once: true }
        )
      }))

  const base = baseUrl.replace(/\/+$/, '')
  const probeHealth = options.probeHealth ?? options.fetchPublicJson
  // The legacy /api/status fallback (for backends predating /api/health) must
  // carry the SAME credentials as the health probe. An oauth remote has a null
  // token and fetchJson attaches no cookie/bearer, so a raw fetchJson here would
  // 401-loop an auth-gated /api/status on a gateway old enough to expose neither
  // /api/health nor a public /api/status — re-opening this very bug via the
  // legacy leg. Route it through the credentialed probe when one was provided.
  const probeStatus = options.probeHealth
    ? (url: string) => options.probeHealth!(url)
    : (url: string) => options.fetchJson(url, options.token)
  const deadline = now() + timeoutMs
  let lastError: unknown = null
  let useStatusFallback = false

  while (now() < deadline) {
    if (signal?.aborted) {
      throw supersededError()
    }

    try {
      if (useStatusFallback) {
        await probeStatus(`${base}/api/status`)
      } else {
        await probeHealth(`${base}/api/health`, { timeoutMs: healthProbeTimeoutMs })
      }

      return
    } catch (error) {
      lastError = error

      // A credentialed probe that came back "needs sign-in" is terminal —
      // polling can't revive an expired session, so surface it immediately (from
      // either leg) for the reauth recovery path instead of burning the timeout.
      if (isReauthRequiredError(error)) {
        throw error
      }

      // Only an explicitly missing route means the backend predates
      // /api/health; timeouts and server errors keep polling health.
      if (!useStatusFallback && isMissingHealthEndpointError(error)) {
        useStatusFallback = true

        continue
      }

      await sleep(pollMs)
    }
  }

  const detail = lastError instanceof Error ? lastError.message : 'timeout'
  throw new Error(`Hermes backend did not become ready: ${detail}`)
}
