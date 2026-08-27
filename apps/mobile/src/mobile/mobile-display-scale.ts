import { Preferences } from '@capacitor/preferences'
import { atom } from 'nanostores'

export interface MobileDisplayScale {
  /** Scales Hermes layout tokens without CSS zooming the WebView. */
  overall: number
  /** Scales readable conversation text while fixed hit targets stay stable. */
  text: number
}

const KEY = 'hermes.mobile.display-scale'
const DEFAULT: MobileDisplayScale = { overall: 1, text: 1 }
const LIMITS = { max: 1.2, min: 0.85, step: 0.05 } as const

export const $mobileDisplayScale = atom<MobileDisplayScale>(DEFAULT)

const clamp = (value: number) => Math.min(LIMITS.max, Math.max(LIMITS.min, Math.round(value * 100) / 100))

function apply(scale: MobileDisplayScale) {
  if (typeof document === 'undefined') return

  const root = document.documentElement
  root.style.setProperty('--dt-base-size', `${scale.overall}rem`)
  root.style.setProperty('--dt-spacing-mul', String(scale.overall))
  root.style.setProperty('--mobile-conversation-text-scale', String(scale.text))
}

async function persist(scale: MobileDisplayScale) {
  try {
    await Preferences.set({ key: KEY, value: JSON.stringify(scale) })
  } catch {
    // Display comfort is local presentation state; a failed persistence write
    // must not undo the visible, safe in-memory adjustment.
  }
}

export function setMobileDisplayScale(next: Partial<MobileDisplayScale>) {
  const current = $mobileDisplayScale.get()
  const scale = { overall: clamp(next.overall ?? current.overall), text: clamp(next.text ?? current.text) }
  $mobileDisplayScale.set(scale)
  apply(scale)
  void persist(scale)
}

export function adjustMobileDisplayScale(kind: keyof MobileDisplayScale, delta: number) {
  setMobileDisplayScale({ [kind]: $mobileDisplayScale.get()[kind] + delta })
}

export function resetMobileDisplayScale() {
  setMobileDisplayScale(DEFAULT)
}

/** Restore the per-device, non-sensitive presentation preference after boot. */
export async function loadMobileDisplayScale() {
  try {
    const stored = await Preferences.get({ key: KEY })
    const parsed = stored.value ? JSON.parse(stored.value) as Partial<MobileDisplayScale> : DEFAULT
    setMobileDisplayScale(parsed)
  } catch {
    apply($mobileDisplayScale.get())
  }
}

export const MOBILE_DISPLAY_SCALE_STEP = LIMITS.step
