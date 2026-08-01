import { useEffect, useRef } from 'react'

import { deriveSessionPetActivity, setPetActivity } from '@/store/pet'
import { setPetScale } from '@/store/pet-gallery'
import { setPetOverlayOpenAppHandler, setPetOverlayScaleHandler, setPetOverlaySubmitHandler } from '@/store/pet-overlay'
import { $sessions, $unreadFinishedSessionIds } from '@/store/session'
import { $attentionSessionIds, $failedSessionIds, $workingSessionIds } from '@/store/session-states'
import { isSecondaryWindow } from '@/store/windows'

import type { GatewayRequester } from '../types'

interface PetBridgeParams {
  requestGateway: GatewayRequester
  resumeSession: (sessionId: string) => Promise<unknown> | unknown
  submitText: (text: string) => Promise<unknown> | unknown
}

/**
 * Wires the popped-out pet overlay back into the app: submit a prompt, resize,
 * and open the most-recent thread, plus collapsing every session's status into
 * the app-global pet pose. Handlers register ONCE through refs tracking the
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

    setPetOverlaySubmitHandler(text => void submitTextRef.current(text))
    // Alt+wheel resize from the popped-out pet — persist through this window's
    // gateway (the overlay has none) so it survives restart.
    setPetOverlayScaleHandler(scale => setPetScale(requestGatewayRef.current, scale))
    // Mail icon: $sessions is most-recent-first; the pet is global, so "most
    // recent" is the right target.
    setPetOverlayOpenAppHandler(() => {
      const recent = $sessions.get()[0]

      if (recent?.id) {
        void resumeSessionRef.current(recent.id)
      }
    })

    return () => {
      setPetOverlaySubmitHandler(null)
      setPetOverlayOpenAppHandler(null)
      setPetOverlayScaleHandler(null)
    }
  }, [])

  // The pet is app-global, like Codex Desktop: one background tile must keep it
  // running even when the primary workspace is idle. Resolve all conversations
  // with the documented priority and mirror the result to the overlay atom.
  useEffect(() => {
    const sync = () =>
      setPetActivity(
        deriveSessionPetActivity({
          attentionSessionIds: $attentionSessionIds.get(),
          failedSessionIds: $failedSessionIds.get(),
          unreadFinishedSessionIds: $unreadFinishedSessionIds.get(),
          workingSessionIds: $workingSessionIds.get()
        })
      )

    sync()

    // These projections keep stable array identities while a turn streams, so
    // token deltas do not churn the pet atom or flood the overlay IPC channel.
    const offAttention = $attentionSessionIds.listen(sync)
    const offFailed = $failedSessionIds.listen(sync)
    const offUnread = $unreadFinishedSessionIds.listen(sync)
    const offWorking = $workingSessionIds.listen(sync)

    return () => {
      offAttention()
      offFailed()
      offUnread()
      offWorking()
    }
  }, [])
}
