import { atom } from 'nanostores'

// A settings overlay can cover the surface a user is tuning. Peeking ghosts the
// armed overlay while retaining its pointer/keyboard ownership, so the live app
// beneath becomes the preview without duplicating that surface in Settings.
// Each owner receives an idempotent disposer: pointerup + lost-capture, stale
// pulse timers, and unrelated controls can never release somebody else's peek.
const PEEK_ATTR = 'data-hermes-overlay-peek'
const owners = new Set<symbol>()

export const $overlayPeek = atom<number>(0)

const publish = () => $overlayPeek.set(owners.size)

export function beginOverlayPeek(): () => void {
  const owner = Symbol('overlay-peek')
  let active = true

  owners.add(owner)
  publish()

  return () => {
    if (!active) {
      return
    }

    active = false
    owners.delete(owner)
    publish()
  }
}

/** Drop every outstanding owner, normally when the Appearance overlay closes. */
export function resetOverlayPeek(): void {
  owners.clear()
  publish()
}

/** Briefly reveal the live surface after a one-shot appearance change. */
export function pulseOverlayPeek(ms = 900): void {
  const release = beginOverlayPeek()

  if (typeof window === 'undefined') {
    release()

    return
  }

  window.setTimeout(release, ms)
}

$overlayPeek.subscribe(count => {
  if (typeof document === 'undefined') {
    return
  }

  document.documentElement.toggleAttribute(PEEK_ATTR, count > 0)
})
