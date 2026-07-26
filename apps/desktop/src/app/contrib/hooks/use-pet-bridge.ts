import { useEffect, useRef } from 'react'

import { submitTextForProfile } from '@/app/session/hooks/use-prompt-actions/profile-submit'
import type { SubmitTextOptions } from '@/app/session/hooks/use-prompt-actions/utils'
import { requestGatewayForProfile } from '@/store/gateway'
import { petProfile } from '@/store/pet'
import { setPetScale } from '@/store/pet-gallery'
import {
  $profilePets,
  clearProfilePetReplyText,
  clearProfilePetUnread,
  setProfileManualAwaitingInput
} from '@/store/pet-multi'
import { setPetOverlayOpenAppHandler, setPetOverlayScaleHandler, setPetOverlaySubmitHandler } from '@/store/pet-overlay'
import { $activeGatewayProfile, normalizeProfileKey } from '@/store/profile'
import { $sessions } from '@/store/session'
import { $attentionSessionIds } from '@/store/session-states'
import { isSecondaryWindow } from '@/store/windows'

import type { GatewayRequester } from '../types'

/** A GatewayRequester bound to one profile (routes through its own socket, never
 *  the active gateway). */
function profileRequest(profile: string): GatewayRequester {
  const key = normalizeProfileKey(profile)

  return (method, params, timeoutMs, signal) =>
    requestGatewayForProfile(key, method, { ...params, profile: key }, timeoutMs, signal)
}

interface PetBridgeParams {
  requestGateway: GatewayRequester
  resumeSession: (sessionId: string) => Promise<unknown> | unknown
  submitText: (text: string, options?: SubmitTextOptions) => Promise<boolean> | boolean
}

/**
 * Wires the popped-out pet overlay back into the app: submit a prompt, resize,
 * and open the most-recent thread, plus mirroring "a session is awaiting the
 * user" into the pet's pose. Handlers register ONCE through refs tracking the
 * latest callbacks — re-registering on identity churn leaves a nulled-handler
 * window that can drop a submit. Primary window only.
 */
export function usePetBridge({ requestGateway, resumeSession, submitText }: PetBridgeParams): void {
  const submitTextRef = useRef(submitText)
  submitTextRef.current = submitText
  const resumeSessionRef = useRef(resumeSession)
  resumeSessionRef.current = resumeSession
  const requestGatewayRef = useRef(requestGateway)
  requestGatewayRef.current = requestGateway

  useEffect(() => {
    if (isSecondaryWindow()) {
      return
    }

    // Overlay submit, addressed by the sender-derived profile. Routes through the
    // shared profile-targeted entry point: the active profile runs the ordinary
    // foreground execution; a background profile runs the SAME pipeline through
    // backgroundSubmitExecution (profile-routed gateway, profile-scoped busy, no
    // foreground writes). A busy session queues under (profile, storedSessionId).
    setPetOverlaySubmitHandler((profile, text) => {
      const key = normalizeProfileKey(profile)
      const state = $profilePets.get().get(key)

      void submitTextForProfile(
        key,
        text,
        { sessionId: state?.sourceSessionId, storedSessionId: state?.sourceDurableSessionId },
        submitTextRef.current
      )
    })
    // Alt+wheel resize from the popped-out pet — persist through the profile's
    // own gateway (the overlay has none) so it survives restart.
    setPetOverlayScaleHandler((profile, scale) => setPetScale(profileRequest(profile), scale))
    // Mail icon: open the overlay's OWN profile session. Prefer its durable
    // source (survives compression/rehoming) — populated from session events via
    // setProfileSourceDurableSessionId. Until a profile has a recorded durable id
    // (e.g. nothing has run yet), the active profile falls back to the
    // most-recent thread ($sessions is most-recent-first) — the legacy single-pet
    // target, keeping follow-active unchanged. Clear that profile's unread/reply
    // (source-session scoped so a different session's unread in the same profile
    // survives).
    setPetOverlayOpenAppHandler(profile => {
      const key = normalizeProfileKey(profile)
      const state = $profilePets.get().get(key)
      const durableId = state?.sourceDurableSessionId

      if (durableId) {
        void resumeSessionRef.current(durableId)
      } else if (key === normalizeProfileKey($activeGatewayProfile.get())) {
        const recent = $sessions.get()[0]

        if (recent?.id) {
          void resumeSessionRef.current(recent.id)
        }
      }

      clearProfilePetUnread(key, state?.sourceSessionId)
      clearProfilePetReplyText(key, state?.sourceSessionId)
    })

    return () => {
      setPetOverlaySubmitHandler(null)
      setPetOverlayOpenAppHandler(null)
      setPetOverlayScaleHandler(null)
    }
  }, [])

  // Mirror "a session is blocked on the user" (clarify/approval) into the active
  // profile's awaitingInput so its pet shows the `waiting` pose. Routed through
  // the manual-awaiting channel (OR-ed into the derived activity) so per-session
  // routing never overwrites it.
  useEffect(() => {
    const sync = () => setProfileManualAwaitingInput(petProfile(), $attentionSessionIds.get().length > 0)

    sync()

    return $attentionSessionIds.listen(sync)
  }, [])
}
