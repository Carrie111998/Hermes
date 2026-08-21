import { atom } from 'nanostores'

// A settings overlay can cover the surface a user is tuning. Peeking ghosts the
// armed overlay while retaining its pointer/keyboard ownership, so the live app
// beneath becomes the preview without duplicating that surface in Settings.
// Count rather than toggle: a held control and one or more timed pulses may
// overlap, and no pulse may close a preview that the user is still holding.
const PEEK_ATTR = 'data-hermes-overlay-peek'
let generation = 0

export const $overlayPeek = atom<number>(0)

export function beginOverlayPeek(): void {
  $overlayPeek.set($overlayPeek.get() + 1)
}

export function endOverlayPeek(): void {
  $overlayPeek.set(Math.max(0, $overlayPeek.get() - 1))
}

/** Drop every outstanding hold/pulse, normally when the owning overlay closes. */
export function resetOverlayPeek(): void {
  generation += 1
  $overlayPeek.set(0)
}

/** Briefly reveal the live surface after a one-shot appearance change. */
export function pulseOverlayPeek(ms = 900): void {
  const pulseGeneration = generation
  beginOverlayPeek()

  if (typeof window === 'undefined') {
    endOverlayPeek()

    return
  }

  window.setTimeout(() => {
    // Reset invalidates every outstanding pulse. Without a generation guard,
    // a stale timer from a closed Settings route could consume a NEW hold after
    // the overlay was reopened.
    if (pulseGeneration === generation) {
      endOverlayPeek()
    }
  }, ms)
}

$overlayPeek.subscribe(count => {
  if (typeof document === 'undefined') {
    return
  }

  document.documentElement.toggleAttribute(PEEK_ATTR, count > 0)
})
