import { useStore } from '@nanostores/react'
import { useEffect, useRef } from 'react'

import { Button } from '@/components/ui/button'
import { SegmentedControl } from '@/components/ui/segmented-control'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { triggerHaptic } from '@/lib/haptics'
import { Eye } from '@/lib/icons'
import { beginOverlayPeek, pulseOverlayPeek } from '@/store/overlay-peek'
import { $sessionListDensity, type SessionListDensity, setSessionListDensity } from '@/store/session-list-density'

import { ListRow } from './primitives'

const DENSITY_PREVIEW_MS = 1_200
const HOLD_KEYS = new Set([' ', 'Enter'])
const POINTER_HOLD_MS = 250

export function SessionDensitySetting() {
  const { t } = useI18n()
  const copy = t.settings.appearance
  const density = useStore($sessionListDensity)
  const keyboardStartedAt = useRef<null | number>(null)
  const pointerStartedAt = useRef<null | number>(null)
  const releaseHold = useRef<null | (() => void)>(null)
  const suppressNextSemanticClick = useRef(false)

  const options = [
    { id: 'compact', label: copy.sessionDensityCompact },
    { id: 'comfortable', label: copy.sessionDensityComfortable },
    { id: 'detailed', label: copy.sessionDensityDetailed }
  ] as const satisfies readonly { id: SessionListDensity; label: string }[]

  const beginHold = () => {
    if (releaseHold.current) {
      return
    }

    releaseHold.current = beginOverlayPeek()
  }

  const endHold = () => {
    releaseHold.current?.()
    releaseHold.current = null
  }

  const cancelPointerHold = () => {
    pointerStartedAt.current = null
    endHold()
  }

  const cancelAllHolds = () => {
    keyboardStartedAt.current = null
    pointerStartedAt.current = null
    suppressNextSemanticClick.current = false
    endHold()
  }

  const endPointerHold = () => {
    const startedAt = pointerStartedAt.current

    pointerStartedAt.current = null
    endHold()

    // A quick tap behaves like ordinary activation; a deliberate hold returns
    // Settings immediately on release instead of starting a second pulse from
    // the click event that browsers dispatch after pointerup.
    if (startedAt !== null && Date.now() - startedAt < POINTER_HOLD_MS) {
      pulseOverlayPeek(DENSITY_PREVIEW_MS)
    }
  }

  const beginKeyboardHold = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (!HOLD_KEYS.has(event.key)) {
      return
    }

    // Own activation instead of letting the native button emit an Enter click
    // on keydown or a Space click after keyup. Assistive click activation still
    // reaches onClick because it has no preceding keyboard event.
    event.preventDefault()

    if (event.repeat || keyboardStartedAt.current !== null) {
      return
    }

    keyboardStartedAt.current = Date.now()
    suppressNextSemanticClick.current = true
    beginHold()
  }

  const endKeyboardHold = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (!HOLD_KEYS.has(event.key)) {
      return
    }

    event.preventDefault()
    const startedAt = keyboardStartedAt.current

    keyboardStartedAt.current = null
    endHold()

    if (startedAt !== null && Date.now() - startedAt < POINTER_HOLD_MS) {
      pulseOverlayPeek(DENSITY_PREVIEW_MS)
    }

    // A browser-generated click, if one survives preventDefault, runs in this
    // activation turn and is suppressed. Clear on the next task so a later
    // assistive click remains a valid bounded preview.
    suppressNextSemanticClick.current = true
    window.setTimeout(() => {
      suppressNextSemanticClick.current = false
    }, 0)
  }

  // A route change or Escape can unmount the button before pointerup/keyup.
  // Release only this control's hold; another appearance control may still own
  // a pulse on the shared counter.
  useEffect(() => endHold, [])

  return (
    <ListRow
      action={
        <div className="flex items-center gap-1.5" data-overlay-peek-scope="">
          <SegmentedControl
            onChange={next => {
              triggerHaptic('selection')
              setSessionListDensity(next)
              pulseOverlayPeek(DENSITY_PREVIEW_MS)
            }}
            options={options}
            value={density}
          />
          <Tip label={copy.sessionDensityPreview}>
            <Button
              aria-label={copy.sessionDensityPreview}
              onBlur={cancelAllHolds}
              onClick={event => {
                // Pointer activation is resolved on pointerup so a long hold
                // does not pulse again. detail=0 covers keyboard and assistive
                // activation, where click is the semantic event.
                if (suppressNextSemanticClick.current) {
                  suppressNextSemanticClick.current = false

                  return
                }

                if (event.detail === 0) {
                  pulseOverlayPeek(DENSITY_PREVIEW_MS)
                }
              }}
              onKeyDown={beginKeyboardHold}
              onKeyUp={endKeyboardHold}
              onLostPointerCapture={cancelPointerHold}
              onPointerCancel={cancelPointerHold}
              onPointerDown={event => {
                if (event.button !== 0) {
                  return
                }

                pointerStartedAt.current = Date.now()
                event.currentTarget.setPointerCapture?.(event.pointerId)
                beginHold()
              }}
              onPointerUp={endPointerHold}
              size="icon-xs"
              type="button"
              variant="ghost"
            >
              <Eye />
            </Button>
          </Tip>
        </div>
      }
      description={copy.sessionDensityDesc}
      title={copy.sessionDensityTitle}
    />
  )
}
