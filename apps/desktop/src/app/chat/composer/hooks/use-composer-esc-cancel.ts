import { useCallback, useEffect, useLayoutEffect, useRef } from 'react'

import { triggerHaptic } from '@/lib/haptics'
import { composerFocusBlockedBySurface } from '@/lib/keybinds/composer-focus-keys'

import { type ComposerTarget, getActiveComposerAddress } from '../focus'
import { useComposerSurfaceId } from '../scope'

interface UseComposerEscCancelOptions {
  awaitingInput: boolean
  busy: boolean
  onCancel: () => unknown
  /** This composer's focus-bus key. With N composers mounted (main + tiles),
   *  only the active one's Esc cancels — otherwise every busy tile stops. */
  target: ComposerTarget
}

/**
 * Global Esc-to-cancel: stop the in-flight turn when the CHAT (not the composer
 * input, which has its own handler) has focus — clicking into the transcript and
 * hitting Esc stops the run, matching the Stop button. A latest-handler ref keeps
 * the window listener registered exactly once while still reading fresh
 * busy/awaitingInput/onCancel each press.
 */
export function useComposerEscCancel({ awaitingInput, busy, onCancel, target }: UseComposerEscCancelOptions) {
  const surfaceId = useComposerSurfaceId()
  // Intentional only: we bail if (a) the composer/another field already handled
  // Esc (defaultPrevented), (b) focus is in any input/textarea/contenteditable
  // (you're typing, not stopping), or (c) a dialog/popover is open — Esc must
  // close that overlay, never double as canceling the stream behind it.
  const escCancelRef = useRef<(event: globalThis.KeyboardEvent) => void>(() => {})

  const escCancel = useCallback(
    (event: globalThis.KeyboardEvent) => {
      // `awaitingInput`: the turn is parked on a clarify / approval / sudo / secret
      // prompt, which owns Esc (or is meant to persist) — never cancel the stream
      // out from under it.
      if (event.key !== 'Escape' || event.defaultPrevented || !busy || awaitingInput) {
        return
      }

      // Only the focused exact composer cancels — target equality is not enough
      // when two visible panes render the same main/session scope.
      const activeComposer = getActiveComposerAddress()

      if (
        activeComposer.target !== target ||
        (surfaceId ? activeComposer.surfaceId !== surfaceId : Boolean(activeComposer.surfaceId))
      ) {
        return
      }

      const active = document.activeElement as HTMLElement | null

      if (active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA' || active.isContentEditable)) {
        return
      }

      // An overlay covering the chat owns Esc (its escape layer closes it) —
      // the composer stays mounted beneath it, so stand down. Same surface
      // signal as type-to-focus; also covers Radix dialogs/popovers.
      if (composerFocusBlockedBySurface()) {
        return
      }

      event.preventDefault()
      triggerHaptic('cancel')
      void Promise.resolve(onCancel())
    },
    [awaitingInput, busy, onCancel, surfaceId, target]
  )

  useLayoutEffect(() => {
    escCancelRef.current = escCancel
  }, [escCancel])

  useEffect(() => {
    const onKeyDown = (event: globalThis.KeyboardEvent) => escCancelRef.current(event)
    window.addEventListener('keydown', onKeyDown)

    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])
}
