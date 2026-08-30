// Shared "this runtime id is gone" latch for every background poller.
//
// When a runtime is reaped the gateway answers session-scoped RPCs with 4001
// ("session not found", tui_gateway `_sess_nowait`). That is TERMINAL for the
// id — no retry can recover it — so each poller must stop re-sending it. The
// `process.list` status-stack poll learned this the hard way (#94219: one id
// accumulated 18,614 rejections in a day), but `approval.pending` and the
// `goal status` slash poll kept hammering, which is what a reaped window's
// steady stream of rejected RPCs came from.
//
// The latch lives here (no imports from ./goals, ./prompts or
// ./composer-status) so all three pollers can share ONE set and, crucially,
// ONE clear path: whatever rebinds a fresh runtime resets every poller at once.
import { JsonRpcGatewayError } from '@hermes/shared'

const goneSessions = new Set<string>()

/** Gateway JSON-RPC code for "session not found" (tui_gateway _sess_nowait). */
const GATEWAY_SESSION_NOT_FOUND_CODE = 4001

/** A gone session is unrecoverable for THIS runtime id; a timeout or transport
 *  blip is not. Only the former may stop the poll — misclassifying a transient
 *  failure would silently freeze the caller on a healthy session.
 *
 *  Match the gateway's 4001 code when the error carries one (JsonRpcGatewayError
 *  from a structured RPC rejection) — a message substring alone could latch on
 *  an unrelated error class that merely mentions "session not found" (e.g. a
 *  wrapped tool/report string). The message fallback survives only for errors
 *  with no numeric code at all, where the frame's structure was lost. */
export function isSessionGoneForBackgroundPolling(error: unknown): boolean {
  if (error instanceof JsonRpcGatewayError && typeof error.code === 'number') {
    return error.code === GATEWAY_SESSION_NOT_FOUND_CODE
  }

  const message = error instanceof Error ? error.message : String(error ?? '')

  return /session not found/i.test(message)
}

/** True while `sid` is latched off — skip the poll entirely. */
export function isSessionGone(sid: string): boolean {
  return goneSessions.has(sid)
}

/** Latch `sid` off until something rebinds a fresh runtime to it. */
export function markSessionGone(sid: string): void {
  goneSessions.add(sid)
}

/** Clear the gone-latch. Called with a session id when a fresh runtime binds to
 *  it (so polling resumes), or with no argument to reset everything (tests). */
export function resetBackgroundPollingGuard(sid?: string): void {
  if (sid) {
    goneSessions.delete(sid)

    return
  }

  goneSessions.clear()
}
