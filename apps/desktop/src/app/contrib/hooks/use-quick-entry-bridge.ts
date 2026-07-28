import { useEffect, useRef } from 'react'

import { useI18n } from '@/i18n'
import { normalizeProfileKey, sessionIdentityKey } from '@/lib/session-identity'
import { notifyError } from '@/store/notifications'
import {
  initQuickEntryBridge,
  type QuickEntrySessionOption,
  setQuickEntrySubmitHandler
} from '@/store/quick-entry'
import { $gatewayState, $sessions } from '@/store/session'
import { sessionTileDelegate } from '@/store/session-states'
import { isSecondaryWindow } from '@/store/windows'

interface QuickEntryBridgeParams {
  startFreshSessionDraft: () => void
  submitText: (text: string) => Promise<unknown> | unknown
}

// The picker is a capture aid, not a session browser — a handful of recent
// rows is the whole point.
const QUICK_ENTRY_SESSION_OPTIONS = 5

function sessionOptions(): QuickEntrySessionOption[] {
  return $sessions
    .get()
    .filter(session => !session.archived)
    .slice(0, QUICK_ENTRY_SESSION_OPTIONS)
    .map(session => ({
      id: sessionIdentityKey(session.id, session.profile),
      target: {
        kind: 'session' as const,
        profile: normalizeProfileKey(session.profile),
        storedSessionId: session.id
      },
      title: session.title?.trim() || session.preview?.trim() || session.id
    }))
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
  const { t } = useI18n()
  const copyRef = useRef(t.desktop)
  copyRef.current = t.desktop
  const submitTextRef = useRef(submitText)
  submitTextRef.current = submitText
  const startFreshRef = useRef(startFreshSessionDraft)
  startFreshRef.current = startFreshSessionDraft

  useEffect(() => {
    if (isSecondaryWindow()) {
      return
    }

    setQuickEntrySubmitHandler(({ target, text }) => {
      if (target.kind === 'new') {
        // Same as the user clicking New Chat and typing: fresh draft, then the
        // normal submit creates the backend session.
        startFreshRef.current()
        void submitTextRef.current(text)

        return
      }

      if (target.kind === 'session') {
        // A picked stored session: resume + submit in the background through
        // the session-tile delegate so the primary view stays where it is.
        const delegate = sessionTileDelegate()

        const targetExists = $sessions
          .get()
          .some(
            session =>
              session.id === target.storedSessionId && normalizeProfileKey(session.profile) === target.profile
          )

        if (!delegate || !targetExists) {
          notifyError(new Error(copyRef.current.sessionUnavailable), copyRef.current.promptFailed)

          return
        }

        void delegate
          .resumeTile(target.storedSessionId, target.profile)
          .then(runtimeId => delegate.submitToSession(runtimeId, text, target.profile))
          // Never redirect a failed owned-session send into the visible chat.
          .catch(error => notifyError(error, copyRef.current.promptFailed))

        return
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
    if (isSecondaryWindow()) {
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

    return () => {
      offGateway()
      offSessions()
    }
  }, [])
}
