import { useStore } from '@nanostores/react'
import { useEffect } from 'react'

import {
  $contextBreakdownBySession,
  type GatewayRequest,
  refreshContextBreakdown
} from '@/store/context-breakdown'
import type { ContextBreakdown } from '@/types/hermes'

interface ContextBreakdownOptions {
  busy: boolean
  enabled: boolean
  requestGateway: GatewayRequest
  sessionId: null | string
}

/** The focused session's context breakdown, fetched as soon as a surface that
 *  shows the gauge is on screen rather than when its popover opens.
 *
 *  The backend only reports measured context occupancy (`last_prompt_tokens`)
 *  once a turn has run in THIS process, so a resumed session reports none —
 *  which is why turning the gauge on used to do nothing at all until you sent
 *  a message. `session.context_breakdown` estimates the same figure from the
 *  live system prompt + tools + transcript, so it answers for a session that
 *  hasn't spoken yet. It is a read-only chars/4 pass: no provider call, no
 *  prompt-cache impact.
 *
 *  Refetches when the focused session changes and when a turn ends (the
 *  transcript just grew). The result lives in a shared per-session store, so
 *  the statusbar gauge and the composer row — which is mounted once per open
 *  tile — collapse onto one request instead of one each. */
export function useContextBreakdown({ busy, enabled, requestGateway, sessionId }: ContextBreakdownOptions): {
  breakdown: ContextBreakdown | null
  loading: boolean
} {
  const bySession = useStore($contextBreakdownBySession)

  useEffect(() => {
    // Mid-turn the transcript changes on every delta and the gateway already
    // streams measured usage, so an estimate would be both stale and wasteful.
    if (!enabled || !sessionId || busy) {
      return
    }

    void refreshContextBreakdown(sessionId, requestGateway)
  }, [busy, enabled, requestGateway, sessionId])

  const entry = sessionId ? bySession[sessionId] : undefined

  // Keyed by the session it describes, so switching sessions drops the
  // previous numbers instead of painting them under the new session's name.
  return { breakdown: entry?.breakdown ?? null, loading: entry?.loading ?? false }
}
