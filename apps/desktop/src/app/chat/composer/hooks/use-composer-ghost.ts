import { useCallback, useEffect, useRef, useState } from 'react'

import type { HermesGateway } from '@/hermes'

/**
 * Ghost-text reply suggestion for the empty composer.
 *
 * When an agent turn completes (busy true → false) and the composer is empty,
 * ask the gateway's deterministic `complete.suggest` for a likely reply and
 * surface the first candidate as ghost text in the placeholder slot. Tab
 * accepts it into real text; typing or a new turn clears it; Esc dismisses it
 * until the next turn. Decorative by contract — any RPC failure resolves to
 * "no ghost", never an error, and never blocks the composer.
 *
 * Backend note: `complete.suggest` is the same gateway RPC the Ink TUI uses;
 * `gateway.request` returns the unwrapped result (matching `useSlashCompletions`).
 */

export interface GhostCandidate {
  kind: string
  text: string
}

interface SuggestResult {
  candidates?: GhostCandidate[]
  history_version?: number
}

/** First candidate's text from a `complete.suggest` result, '' when none. */
export function firstGhostCandidate(result: unknown): string {
  const text = (result as SuggestResult | null | undefined)?.candidates?.[0]?.text

  return typeof text === 'string' ? text.trim() : ''
}

/** Whether the ghost should render: only in an idle, empty, undismissed
 *  composer that actually has a suggestion. Pure so the contract is testable. */
export function ghostVisible(o: { busy: boolean; dismissed: boolean; empty: boolean; ghost: string }): boolean {
  return o.empty && !o.busy && !o.dismissed && o.ghost !== ''
}

export function useComposerGhost(opts: {
  busy: boolean
  empty: boolean
  gateway: HermesGateway | null
  sessionId: null | string | undefined
}): {
  acceptGhost: () => null | string
  dismissGhost: () => boolean
  ghost: string
} {
  const { busy, empty, gateway, sessionId } = opts
  const [ghost, setGhost] = useState('')
  const [dismissed, setDismissed] = useState(false)
  const prevBusyRef = useRef(busy)
  const seqRef = useRef(0)

  // Fire exactly once per completed turn: on the busy → idle transition.
  useEffect(() => {
    const wasBusy = prevBusyRef.current
    prevBusyRef.current = busy

    if (!wasBusy || busy) {
      return
    }

    if (!gateway || !sessionId || !empty) {
      return
    }

    const seq = ++seqRef.current

    setDismissed(false)
    setGhost('')

    gateway
      .request<SuggestResult>('complete.suggest', { session_id: sessionId })
      .then(result => {
        if (seq === seqRef.current) {
          setGhost(firstGhostCandidate(result))
        }
      })
      .catch(() => {
        // Decorative: a failed suggestion must never reach the composer.
      })
  }, [busy, empty, gateway, sessionId])

  // Typing (empty → false) clears the ghost so it never overlaps real text.
  useEffect(() => {
    if (!empty) {
      setGhost('')
      setDismissed(false)
    }
  }, [empty])

  // A different conversation invalidates any in-flight or shown suggestion.
  useEffect(() => {
    seqRef.current += 1
    setGhost('')
    setDismissed(false)
  }, [sessionId])

  const visible = ghostVisible({ busy, dismissed, empty, ghost })

  const acceptGhost = useCallback((): null | string => {
    if (!visible) {
      return null
    }

    setGhost('')

    return ghost
  }, [ghost, visible])

  const dismissGhost = useCallback((): boolean => {
    if (!visible) {
      return false
    }

    setDismissed(true)

    return true
  }, [visible])

  return { acceptGhost, dismissGhost, ghost: visible ? ghost : '' }
}
