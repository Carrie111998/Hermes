import { useEffect } from 'react'

import { listAllProfileSessions } from '@/hermes'
import { translateNow } from '@/i18n'
import { parseWakeVoiceRoute, resolveWakeVoiceRouteCommand } from '@/lib/wake-voice-routing'
import { notify, notifyError } from '@/store/notifications'
import { $activeGatewayProfile, normalizeProfileKey } from '@/store/profile'
import { sessionTileDelegate } from '@/store/session-states'
import { setWakeVoiceRouteHandler } from '@/store/wake-voice-routing'
import { isSecondaryWindow } from '@/store/windows'

const NOTIFICATION_ID = 'wake-voice-session-routing'
// The cross-profile endpoint intentionally caps each profile at 500 rows. We
// request that complete safe window and compare it with profile_totals before
// resolving; profiles beyond the cap fail closed rather than guessing.
const SAFE_SESSION_LIST_LIMIT = 500

function profileTotal(profileTotals: Record<string, number> | undefined, profile: string, fallback: number): number {
  const entry = Object.entries(profileTotals ?? {}).find(([key]) => normalizeProfileKey(key) === profile)

  return entry?.[1] ?? fallback
}

/**
 * Gives first-turn Wake Word commands a safe, background session-submit path.
 * The ordinary voice transcript path stays untouched when the utterance is not
 * an explicit routing command.
 */
export function useWakeVoiceRoutingBridge(): void {
  useEffect(() => {
    if (isSecondaryWindow()) {
      return
    }

    setWakeVoiceRouteHandler(async (transcript, requestedProfile) => {
      const command = parseWakeVoiceRoute(transcript)

      if (command.kind === 'none') {
        return 'not-route'
      }

      if (command.kind === 'invalid') {
        notify({
          id: NOTIFICATION_ID,
          kind: 'warning',
          message:
            command.reason === 'missing_prompt'
              ? translateNow('notifications.voice.routeMissingPrompt', command.destination)
              : translateNow('notifications.voice.routeInvalidSyntax')
        })

        return 'rejected'
      }

      const profile = normalizeProfileKey(requestedProfile || $activeGatewayProfile.get())
      let sessions

      try {
        const result = await listAllProfileSessions(SAFE_SESSION_LIST_LIMIT, 1, 'exclude', 'recent', profile)
        const targetError = result.errors?.find(error => normalizeProfileKey(error.profile) === profile)
        const targetSessions = result.sessions.filter(session => normalizeProfileKey(session.profile) === profile)
        const total = profileTotal(result.profile_totals, profile, result.total)

        if (targetError || targetSessions.length < total) {
          notify({
            id: NOTIFICATION_ID,
            kind: 'warning',
            message: translateNow('notifications.voice.routeUnavailable')
          })

          return 'rejected'
        }

        sessions = targetSessions
      } catch (error) {
        notifyError(error, translateNow('notifications.voice.routeUnavailable'))

        return 'rejected'
      }

      const resolution = resolveWakeVoiceRouteCommand(command, sessions, profile)

      if (resolution.kind === 'missing') {
        notify({
          id: NOTIFICATION_ID,
          kind: 'warning',
          message: translateNow('notifications.voice.routeNotFound', resolution.destination)
        })

        return 'rejected'
      }

      if (resolution.kind === 'ambiguous') {
        notify({
          detail: resolution.candidates.join('\n'),
          id: NOTIFICATION_ID,
          kind: 'warning',
          message: translateNow('notifications.voice.routeAmbiguous', resolution.destination)
        })

        return 'rejected'
      }

      if (resolution.kind !== 'match') {
        return 'rejected'
      }

      const delegate = sessionTileDelegate()

      if (!delegate) {
        notify({
          id: NOTIFICATION_ID,
          kind: 'warning',
          message: translateNow('notifications.voice.routeUnavailable')
        })

        return 'rejected'
      }

      try {
        const runtimeId = await delegate.resumeTile(resolution.sessionId, profile)
        await delegate.submitToSession(runtimeId, resolution.prompt)
        notify({
          id: NOTIFICATION_ID,
          kind: 'success',
          message: translateNow('notifications.voice.routedToSession', resolution.title)
        })

        return 'routed'
      } catch (error) {
        notifyError(error, translateNow('notifications.voice.routeFailed', resolution.title))

        return 'rejected'
      }
    })

    return () => setWakeVoiceRouteHandler(null)
  }, [])
}
