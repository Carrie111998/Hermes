import { atom } from 'nanostores'

import type { ContextBreakdown } from '@/types/hermes'

export type GatewayRequest = <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>

export interface ContextBreakdownEntry {
  breakdown: ContextBreakdown | null
  loading: boolean
}

const EMPTY: ContextBreakdownEntry = { breakdown: null, loading: false }

/**
 * Per-session context breakdown, shared by every surface that shows the gauge.
 *
 * Today that is the statusbar item alone, refetched on every turn end and every
 * session switch. The store rather than a per-caller `useState` because the RPC
 * is not free on the backend — it rebuilds the system prompt from disk and walks
 * the transcript — so a second gauge must not double it: one in-flight request
 * per session, every subscriber reading the same answer, and the numbers survive
 * a remount.
 */
export const $contextBreakdownBySession = atom<Record<string, ContextBreakdownEntry>>({})

const inFlight = new Map<string, Promise<void>>()

function patch(sessionId: string, entry: Partial<ContextBreakdownEntry>): void {
  const current = $contextBreakdownBySession.get()

  $contextBreakdownBySession.set({
    ...current,
    [sessionId]: { ...(current[sessionId] ?? EMPTY), ...entry }
  })
}

/** The entry for a session, or a stable empty one.
 *
 *  Keyed by the session it describes, so switching sessions drops the previous
 *  numbers instead of painting them under the new session's name. `bySession`
 *  is a parameter so a React caller can pass the snapshot it is already
 *  subscribed to rather than reading the atom a second time. */
export function contextBreakdownFor(
  sessionId: null | string,
  bySession: Record<string, ContextBreakdownEntry> = $contextBreakdownBySession.get()
): ContextBreakdownEntry {
  return (sessionId && bySession[sessionId]) || EMPTY
}

export async function refreshContextBreakdown(sessionId: string, request: GatewayRequest): Promise<void> {
  const pending = inFlight.get(sessionId)

  if (pending) {
    return pending
  }

  patch(sessionId, { loading: true })

  const run = (async () => {
    try {
      const breakdown = await request<ContextBreakdown>('session.context_breakdown', { session_id: sessionId })

      if (breakdown) {
        patch(sessionId, { breakdown, loading: false })

        return
      }

      patch(sessionId, { loading: false })
    } catch {
      // Transient socket loss — the next turn end or session switch retries.
      // Keep the previous numbers rather than blanking the gauge.
      patch(sessionId, { loading: false })
    } finally {
      inFlight.delete(sessionId)
    }
  })()

  inFlight.set(sessionId, run)

  return run
}

/** Test seam — module state outlives any single component. */
export function _resetContextBreakdownForTests(): void {
  inFlight.clear()
  $contextBreakdownBySession.set({})
}
