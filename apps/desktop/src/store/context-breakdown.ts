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
 * There are two of them now — the statusbar item and the composer row — and
 * the composer is mounted once per open tile, so a per-caller `useState` meant
 * N identical `session.context_breakdown` RPCs on every turn end. The call is
 * not free on the backend either: it rebuilds the system prompt from disk and
 * walks the transcript. One store, one in-flight request per session, every
 * subscriber reading the same answer.
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

export function contextBreakdownFor(sessionId: null | string): ContextBreakdownEntry {
  return (sessionId && $contextBreakdownBySession.get()[sessionId]) || EMPTY
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
