import { useStore } from '@nanostores/react'
import { useEffect } from 'react'

import {
  $contextBreakdownBySession,
  contextBreakdownFor,
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
 *  a second gauge on screen reads the same answer instead of issuing its own
 *  request, and the numbers survive a remount. */
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

  const { breakdown, loading } = contextBreakdownFor(sessionId, bySession)

  return { breakdown, loading }
}
