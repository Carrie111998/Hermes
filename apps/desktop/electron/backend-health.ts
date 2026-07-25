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

export interface HermesReadyOptions {
  fetchPublicJson: FetchPublicJson
  fetchJson: FetchJson
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

  // 404 / HTML: route genuinely absent (SPA returns JSON 404 for unknown /api/*).
  // Gated 401 no_cookie: pre-/api/health backends (e.g. release 0.19.0) run the
  // auth gate before the SPA catch-all, so an unknown /api/health is rejected as
  // unauthenticated instead of 404. Only that gate shape falls back — a generic
  // 401 must not skip a real (misconfigured) health endpoint.
  const gatedMissingHealth =
    /^401:/.test(message) && (message.includes('"reason":"no_cookie"') || message.includes('no_cookie'))

  return (
    /^404:/.test(message) ||
    gatedMissingHealth ||
    message.includes('endpoint is likely missing')
  )
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
  const deadline = now() + timeoutMs
  let lastError: unknown = null
  let useStatusFallback = false

  while (now() < deadline) {
    if (signal?.aborted) {
      throw supersededError()
    }

    try {
      if (useStatusFallback) {
        await options.fetchJson(`${base}/api/status`, options.token)
      } else {
        await options.fetchPublicJson(`${base}/api/health`, { timeoutMs: healthProbeTimeoutMs })
      }

      return
    } catch (error) {
      lastError = error

      // Missing or gate-rejected /api/health means the backend predates the
      // public health probe (404, or gated 401 no_cookie on older builds).
      // Timeouts, 5xx, and other 401s keep polling health.
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
