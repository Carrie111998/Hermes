import { useEffect, useRef } from 'react'

import { notifyError } from '@/store/notifications'
import {
  initQuickEntryBridge,
  QUICK_TARGET_CURRENT,
  QUICK_TARGET_NEW,
  type QuickEntrySessionOption,
  setQuickEntrySubmitHandler
} from '@/store/quick-entry'
import { $gatewayState, $sessions } from '@/store/session'
import { $sessionTiles, sessionTileDelegate, sessionTileIdentity } from '@/store/session-states'
import { isAuxiliaryWindow } from '@/store/windows'

import { sessionRowOwnerRoute } from '../../chat/session-row-owner'

interface QuickEntryBridgeParams {
  startFreshSessionDraft: () => void
  submitText: (text: string) => Promise<unknown> | unknown
}

// The picker is a capture aid, not a session browser — a handful of recent
// rows is the whole point.
const QUICK_ENTRY_SESSION_OPTIONS = 5

function sessionOptions(): QuickEntrySessionOption[] {
  const tiles = $sessionTiles.get()

  return $sessions
    .get()
    .filter(session => !session.archived)
    .slice(0, QUICK_ENTRY_SESSION_OPTIONS)
    .map(session => {
      const rowOwnerRoute = sessionRowOwnerRoute(session)
      const target = sessionTileIdentity(session.id, rowOwnerRoute)

      const tile = tiles.find(
        candidate => sessionTileIdentity(candidate.storedSessionId, candidate.ownerRoute) === target
      )

      const ownerRoute = tile?.ownerRoute ?? rowOwnerRoute

      return {
        id: session.id,
        ...(tile ? { ownerGeneration: tile.ownerGeneration ?? 0 } : {}),
        ...(ownerRoute ? { ownerRoute, target } : {}),
        title: session.title?.trim() || session.preview?.trim() || session.id
      }
    })
}

/**
 * Wires the global-hotkey Quick Entry window back into the app, both ways:
 *
 * - **Inbound:** text captured there is routed by target and submitted through
 *   THIS window's normal prompt machinery — current chat rides `submitText`, a
 *   picked stored session rides the session-tile delegate (resume + submit,
 *   background, without touching the primary view — the same path tiled
 *   sessions use), and "new session" is a fresh draft + submit, exactly what
 *   clicking New Chat and typing does. One submit pipeline, no bespoke RPC.
 * - **Outbound:** gateway connection state + the recent-session list are pushed
 *   to the quick window (via main, which caches the latest push), so its input
 *   disables with a reconnect hint whenever the backend is unreachable.
 *
 * Handlers register ONCE through refs tracking the latest callbacks —
 * re-registering on identity churn leaves a nulled-handler window that can drop
 * a submit (the same bug shape use-pet-bridge guards). Primary window only: a
 * secondary session window must not also claim the global capture channel, or
 * one keystroke would send N prompts.
 */
export function useQuickEntryBridge({ startFreshSessionDraft, submitText }: QuickEntryBridgeParams): void {
  const submitTextRef = useRef(submitText)
  submitTextRef.current = submitText
  const startFreshRef = useRef(startFreshSessionDraft)
  startFreshRef.current = startFreshSessionDraft

  useEffect(() => {
    if (isAuxiliaryWindow()) {
      return
    }

    setQuickEntrySubmitHandler(({ session, target, text }) => {
      if (target === QUICK_TARGET_NEW) {
        // Same as the user clicking New Chat and typing: fresh draft, then the
        // normal submit creates the backend session.
        startFreshRef.current()
        void submitTextRef.current(text)

        return
      }

      if (target !== QUICK_TARGET_CURRENT) {
        // A picked stored session: resume + submit in the background through
        // the session-tile delegate so the primary view stays where it is.
        const delegate = sessionTileDelegate()

        if (delegate) {
          const storedSessionId = session?.id || target

          const resume = session
            ? delegate.resumeTile(storedSessionId, session.ownerGeneration, session.ownerRoute)
            : delegate.resumeTile(storedSessionId)

          void resume
            .then(runtimeId => delegate.submitToSession(runtimeId, text))
            .catch(error => {
              // Exact picked metadata is an authority boundary: redirecting its
              // prompt into whichever ambient composer happens to be selected
              // can send private text to the wrong backend/session. Surface the
              // failed handoff instead. Keep the legacy metadata-free fallback
              // for old quick windows whose raw-id payload cannot name a target.
              if (session) {
                notifyError(error, 'Quick Entry could not reach the selected session')

                return
              }

              void submitTextRef.current(text)
            })

          return
        }

        if (session) {
          notifyError(
            new Error('Quick Entry background session bridge is unavailable'),
            'Quick Entry could not reach the selected session'
          )

          return
        }
      }

      void submitTextRef.current(text)
    })

    const dispose = initQuickEntryBridge()

    return () => {
      setQuickEntrySubmitHandler(null)
      dispose()
    }
  }, [])

  // Push gateway truth into the quick window whenever it changes: connection
  // state gates its input; the recent-session list feeds its target picker.
  useEffect(() => {
    if (isAuxiliaryWindow()) {
      return
    }

    const api = window.hermesDesktop?.quickEntry

    if (!api?.pushState) {
      return
    }

    const push = () => {
      api.pushState({ connected: $gatewayState.get() === 'open', sessions: sessionOptions() })
    }

    push()

    const offGateway = $gatewayState.listen(push)
    const offSessions = $sessions.listen(push)
    const offTiles = $sessionTiles.listen(push)

    return () => {
      offGateway()
      offSessions()
      offTiles()
    }
  }, [])
}
