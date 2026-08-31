import { JsonRpcGatewayError } from '@hermes/shared'

const GATEWAY_SESSION_NOT_FOUND_CODE = 4001
const goneSessions = new Set<string>()

/** True only for a terminal session-scoped "not found" rejection. */
export function isSessionNotFoundError(error: unknown): boolean {
  if (error instanceof JsonRpcGatewayError && typeof error.code === 'number') {
    return error.code === GATEWAY_SESSION_NOT_FOUND_CODE
  }

  const message = (error instanceof Error ? error.message : String(error ?? ''))
    .trim()
    .replace(/^Error invoking remote method '[^']+':\s*Error:\s*/i, '')
    .replace(/^Error:\s*/i, '')

  return /^(?:4001\s*[:,-]?\s*)?session not found[.!]?$/i.test(message)
}

/** Whether session-scoped RPCs should stop targeting this runtime id. */
export function isSessionRpcBlocked(sessionId: null | string | undefined): boolean {
  return Boolean(sessionId && goneSessions.has(sessionId))
}

/** Latch a runtime id after a terminal 4001/session-not-found response. */
export function markSessionRpcBlocked(sessionId: null | string | undefined): void {
  if (sessionId) {
    goneSessions.add(sessionId)
  }
}

/** Clear one rebound runtime id, or all ids at a reconnect boundary. */
export function resetSessionRpcGuard(sessionId?: string): void {
  if (sessionId) {
    goneSessions.delete(sessionId)
  } else {
    goneSessions.clear()
  }
}

/**
 * A socket reconnect is not a rebind: the backend may have reaped the old
 * runtime, and merely opening a WebSocket does not make that id valid again.
 * Only a successful resume/activate response is proof that the runtime can be
 * targeted again.
 */
export function resetSessionRpcGuardAfterRebind(
  method: string,
  params: Record<string, unknown>,
  result: unknown
): void {
  if (method !== 'session.activate' && method !== 'session.resume') {
    return
  }

  const ids = new Set<string>()
  const add = (value: unknown) => {
    if (typeof value === 'string' && value.trim()) {
      ids.add(value.trim())
    }
  }

  add(params.session_id)

  if (result && typeof result === 'object') {
    add((result as { session_id?: unknown }).session_id)
  }

  for (const id of ids) {
    resetSessionRpcGuard(id)
  }
}
