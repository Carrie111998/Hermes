import { useEffect, useRef } from 'react'

import { setPetActivity } from '@/store/pet'
import { setPetScale } from '@/store/pet-gallery'
import {
  dockAvatar,
  setAvatarHidden,
  setAvatarSize,
  setPetOverlayDockHandler,
  setPetOverlayHideHandler,
  setPetOverlayOpenAppHandler,
  setPetOverlayOpenChatHandler,
  setPetOverlayOpenSettingsHandler,
  setPetOverlayQuitHandler,
  setPetOverlayScaleHandler,
  setPetOverlaySetSizeHandler,
  setPetOverlaySubmitHandler
} from '@/store/pet-overlay'
import { $sessions } from '@/store/session'
import { $attentionSessionIds } from '@/store/session-states'
import { isSecondaryWindow } from '@/store/windows'

import type { GatewayRequester } from '../types'

interface PetBridgeParams {
  navigate?: (path: string) => void
  requestGateway: GatewayRequester
  resumeSession: (sessionId: string) => Promise<unknown> | unknown
  submitText: (text: string) => Promise<unknown> | unknown
}

/**
 * Wires the popped-out pet overlay back into the app: submit a prompt, resize,
 * and open the most-recent thread, plus mirroring "a session is awaiting the
 * user" into the pet's pose. Handlers register ONCE through refs tracking the
 * latest callbacks — re-registering on identity churn leaves a nulled-handler
 * window that can drop a submit. Primary window only.
 */
export function usePetBridge({ navigate, requestGateway, resumeSession, submitText }: PetBridgeParams): void {
  const submitTextRef = useRef(submitText)
  submitTextRef.current = submitText
  const resumeSessionRef = useRef(resumeSession)
  resumeSessionRef.current = resumeSession
  const requestGatewayRef = useRef(requestGateway)
  requestGatewayRef.current = requestGateway
  const navigateRef = useRef(navigate)
  navigateRef.current = navigate

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
    // Hide avatar: persist hidden flag so it stays hidden across sessions.
    setPetOverlayHideHandler(() => setAvatarHidden(true))
    // Quit: close the overlay window (same as pop-in, but explicitly user-initiated).
    setPetOverlayQuitHandler(() => {
      // This will trigger popInPet via the control bridge.
      window.hermesDesktop?.petOverlay?.close()
    })
    // Open chat: focus the app window on the current/most recent session.
    setPetOverlayOpenChatHandler(() => {
      const recent = $sessions.get()[0]

      if (recent?.id) {
        void resumeSessionRef.current(recent.id)
      }
    })
    // Open settings: navigate to settings page in the main window.
    setPetOverlayOpenSettingsHandler(() => {
      navigateRef.current?.('/settings')
    })
    // P0.1: Dock → set mode to docked and close the overlay window.
    setPetOverlayDockHandler(() => {
      dockAvatar()
    })
    // P0.3: Size preset change from overlay → persist via $avatarSize.
    // The subscription on $avatarSize fires pushNow, so the overlay receives
    // the new size on the next state push (confirming its local change) and
    // effectiveScale (derived from avatarSize) drives the sprite + window resize.
    setPetOverlaySetSizeHandler(size => setAvatarSize(size))

    return () => {
      setPetOverlaySubmitHandler(null)
      setPetOverlayOpenAppHandler(null)
      setPetOverlayScaleHandler(null)
      setPetOverlayHideHandler(null)
      setPetOverlayQuitHandler(null)
      setPetOverlayOpenChatHandler(null)
      setPetOverlayOpenSettingsHandler(null)
      setPetOverlayDockHandler(null)
      setPetOverlaySetSizeHandler(null)
    }
  }, [])

  // Mirror "a session is blocked on the user" (clarify/approval) into the pet's
  // awaitingInput flag so it shows the `waiting` pose.
  useEffect(() => {
    const sync = () => setPetActivity({ awaitingInput: $attentionSessionIds.get().length > 0 })

    sync()

    return $attentionSessionIds.listen(sync)
  }, [])
}
