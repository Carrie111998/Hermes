import { isGatewayAuthRejection } from './connection-config'

const DEFAULT_REMOTE_CONNECTION_RETRY_TIMEOUT_MS = 45_000
const DEFAULT_REMOTE_READY_ATTEMPT_TIMEOUT_MS = 8_000

interface RemoteConnectionAttemptContext {
  readonly attempt: number
  readonly elapsedMs: number
  readonly remainingMs: number
}

interface RemoteConnectionRetryOptions {
  initialDelayMs?: number
  maxAttempts?: number
  maxDelayMs?: number
  maxElapsedMs?: number
  now?: () => number
  onRetry?: (error: unknown, attempt: number, delayMs: number) => void
  sleep?: (delayMs: number) => Promise<void>
}

function remoteHttpStatusError(statusCode: number | undefined, detail: unknown): Error & { statusCode: number } {
  return Object.assign(new Error(`${statusCode}: ${String(detail || '')}`), { statusCode: statusCode || 500 })
}

function requiresOauthLogin(error: unknown, seen = new Set<object>()): boolean {
  if (!error || typeof error !== 'object' || seen.has(error)) {
    return false
  }

  seen.add(error)
  const candidate = error as { cause?: unknown }

  return isGatewayAuthRejection(error) || Boolean(candidate.cause && requiresOauthLogin(candidate.cause, seen))
}

const RETRYABLE_NETWORK_CODES = new Set([
  'EAI_AGAIN',
  'ECONNREFUSED',
  'ECONNRESET',
  'EHOSTUNREACH',
  'ENETDOWN',
  'ENETUNREACH',
  'ETIMEDOUT',
  'ERR_INTERNET_DISCONNECTED',
  'UND_ERR_CONNECT_TIMEOUT',
  'UND_ERR_SOCKET'
])

/** True only for failures that can plausibly clear without changing config. */
function isRetryableRemoteConnectionError(error: unknown, seen = new Set<object>()): boolean {
  if (!error || typeof error !== 'object' || requiresOauthLogin(error) || seen.has(error)) {
    return false
  }

  seen.add(error)

  const candidate = error as {
    cause?: unknown
    code?: unknown
    kind?: unknown
    message?: unknown
    statusCode?: unknown
  }

  const statusCode = Number(candidate.statusCode)

  if (statusCode === 408 || statusCode === 425 || statusCode === 429 || (statusCode >= 500 && statusCode <= 599)) {
    return true
  }

  if (
    candidate.kind === 'timeout' ||
    candidate.kind === 'transient-transport-error' ||
    candidate.kind === 'unreachable'
  ) {
    return true
  }

  if (typeof candidate.code === 'string' && RETRYABLE_NETWORK_CODES.has(candidate.code.toUpperCase())) {
    return true
  }

  if (candidate.cause && isRetryableRemoteConnectionError(candidate.cause, seen)) {
    return true
  }

  // Some timeout wrappers (including the desktop's bounded ticket request)
  // carry no structured code. Keep this narrow: configuration messages such as
  // invalid URLs, missing tokens, invalid SSH settings, and auth failures do not
  // match and therefore fail immediately.
  return typeof candidate.message === 'string' && /(?:timed? out|timeout)/i.test(candidate.message)
}

function retryDeadlineError(lastError: unknown, maxElapsedMs: number): Error & { kind: 'timeout' } {
  const detail = lastError instanceof Error ? lastError.message : String(lastError || 'unknown error')

  const error = new Error(`Remote connection retry deadline exceeded after ${maxElapsedMs}ms: ${detail}`, {
    cause: lastError
  }) as Error & { kind: 'timeout' }

  error.kind = 'timeout'

  return error
}

/**
 * A controlled backend restart can leave the public route unavailable for
 * more than 20 seconds. Retry only ordinary transport/server failures; a
 * positively classified 401/403 must immediately surface the sign-in flow
 * instead of being hidden by backoff.
 *
 * Calling `resolve` again is intentional: OAuth WebSocket tickets are
 * single-use, so every attempt must mint a fresh ticket rather than reuse one
 * captured by an earlier attempt.
 */
async function resolveRemoteConnectionWithRetry<T>(
  resolve: (context: RemoteConnectionAttemptContext) => Promise<T>,
  {
    initialDelayMs = 500,
    maxAttempts = 9,
    maxDelayMs = 4_000,
    maxElapsedMs = DEFAULT_REMOTE_CONNECTION_RETRY_TIMEOUT_MS,
    now = Date.now,
    onRetry,
    sleep = delayMs => new Promise<void>(done => setTimeout(done, delayMs))
  }: RemoteConnectionRetryOptions = {}
): Promise<T> {
  const attempts = Math.max(1, Math.floor(maxAttempts))
  const budgetMs = Math.max(1, Math.floor(maxElapsedMs))
  const startedAt = now()
  const deadline = startedAt + budgetMs
  let delayMs = Math.max(0, initialDelayMs)
  let lastError: unknown = null

  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    const context: RemoteConnectionAttemptContext = {
      attempt,
      get elapsedMs() {
        return Math.max(0, now() - startedAt)
      },
      get remainingMs() {
        return Math.max(0, deadline - now())
      }
    }

    try {
      return await resolve(context)
    } catch (error) {
      lastError = error

      if (attempt >= attempts || !isRetryableRemoteConnectionError(error)) {
        throw error
      }

      if (context.remainingMs <= 0) {
        throw retryDeadlineError(lastError, budgetMs)
      }

      const boundedDelayMs = Math.min(delayMs, context.remainingMs)

      onRetry?.(error, attempt, boundedDelayMs)
      await sleep(boundedDelayMs)

      if (now() >= deadline) {
        throw retryDeadlineError(lastError, budgetMs)
      }

      delayMs = Math.min(Math.max(delayMs * 2, initialDelayMs), maxDelayMs)
    }
  }

  throw new Error('Remote connection retry loop exhausted unexpectedly')
}

async function resolveReadyRemoteConnectionWithRetry<T>(
  resolve: (context: RemoteConnectionAttemptContext) => Promise<null | T>,
  waitForReady: (connection: T, context: RemoteConnectionAttemptContext) => Promise<void>,
  options: RemoteConnectionRetryOptions = {}
): Promise<null | T> {
  return resolveRemoteConnectionWithRetry(async context => {
    const connection = await resolve(context)

    if (connection) {
      await waitForReady(connection, context)
    }

    return connection
  }, options)
}

export {
  DEFAULT_REMOTE_CONNECTION_RETRY_TIMEOUT_MS,
  DEFAULT_REMOTE_READY_ATTEMPT_TIMEOUT_MS,
  isRetryableRemoteConnectionError,
  remoteHttpStatusError,
  requiresOauthLogin,
  resolveReadyRemoteConnectionWithRetry,
  resolveRemoteConnectionWithRetry
}
export type { RemoteConnectionAttemptContext, RemoteConnectionRetryOptions }
