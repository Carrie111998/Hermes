import { atom } from 'nanostores'

import { persistBoolean, persistString, storedBoolean, storedString } from '@/lib/storage'
import {
  $petActivity,
  $petInfo,
  $petReplyText,
  $petUnread,
  clearPetUnread,
  clearPetReplyText,
  type PetActivity,
  type PetInfo,
  petProfile
} from '@/store/pet'
import { $profilePets, type ProfileConnection } from '@/store/pet-multi'
import { normalizeProfileKey } from '@/store/profile'
import { $awaitingResponse, $busy } from '@/store/session'

/**
 * Controller for the pop-out pet overlay (main-renderer side).
 *
 * Shift-clicking the in-window pet "pops it out" into a transparent,
 * always-on-top OS window (created in electron/main.ts) that can leave the
 * app's bounds and stays visible while Hermes is minimized. That window carries
 * NO gateway connection — this renderer remains the single source of truth and
 * pushes the live pet state to it over IPC. Control flows back (pop the pet back
 * in, submit a composer message) via `onControl`.
 *
 * The overlay renders the same `PetSprite` / `PetBubble` as the in-window pet by
 * mirroring the four reactive inputs of `$petState` (`$petInfo`, `$petActivity`,
 * `$busy`, `$awaitingResponse`) into its own copies of those atoms — so the
 * popped-out mascot is pixel-identical and needs zero bespoke render logic.
 */

export interface PetOverlayBounds {
  x: number
  y: number
  width: number
  height: number
}

/**
 * Request to open the overlay window. `screen` says whether `bounds` are already
 * in absolute screen coordinates (a remembered/dragged spot) or in the main
 * window's viewport space (a fresh shift-click pop-out, which main.ts converts
 * by adding the content origin).
 */
export interface PetOverlayOpenRequest {
  bounds: PetOverlayBounds
  screen?: boolean
}

/** Everything the overlay needs to reproduce the live mascot. */
export interface PetOverlayStatePayload {
  info: PetInfo
  activity: PetActivity
  busy: boolean
  awaiting: boolean
  /** Drives the overlay's mail icon: a finish landed while you were away. */
  unread: boolean
  /** Latest reply text to show in the speech bubble instead of the mail icon. */
  replyText: string | null
  /** Latest reaction - bumping its id forwards a burst to the overlay. */
  reaction: PetReaction | null
  /** The profile's gateway connection state — the overlay desaturates + badges
   *  a disconnect glyph when offline/reauth so a down backend never looks happy. */
  connection: ProfileConnection
}

export type PetOverlayControl =
  | { type: 'pop-in' }
  | { type: 'ready' }
  | { type: 'submit'; text: string }
  | { type: 'bounds'; bounds: PetOverlayBounds }
  | { type: 'open-app' }
  | { type: 'toggle-app' }
  | { type: 'scale'; scale: number }

// Persisted across restarts: was the pet popped out, and where on the desktop
// did the user leave it. Keyed v1; bump if the bounds shape ever changes.
const OVERLAY_ACTIVE_KEY = 'hermes.desktop.pet-overlay-active.v1'
const OVERLAY_BOUNDS_KEY = 'hermes.desktop.pet-overlay-bounds.v1'

export const $petOverlayActive = atom(storedBoolean(OVERLAY_ACTIVE_KEY, false))

// Persist the in/out choice so a popped-out pet comes back popped out.
$petOverlayActive.subscribe(active => persistBoolean(OVERLAY_ACTIVE_KEY, active))

/**
 * Reaction signal forwarded to the popped-out overlay window via the state
 * mirror below. `id` is a monotonic nonce so the overlay fires once per bump;
 * `kind` selects the renderer (today only `vibe` → hearts). Generic on purpose
 * so future reactions (emoji, etc.) ride the same channel.
 */
export interface PetReaction {
  id: number
  kind: string
}

export const $petReaction = atom<PetReaction | null>(null)

export const forwardPetReaction = (kind: string) => $petReaction.set({ id: ($petReaction.get()?.id ?? 0) + 1, kind })

function loadSavedBounds(): null | PetOverlayBounds {
  try {
    const raw = storedString(OVERLAY_BOUNDS_KEY)

    if (!raw) {
      return null
    }

    const parsed = JSON.parse(raw) as Partial<PetOverlayBounds>

    if (
      typeof parsed.x === 'number' &&
      typeof parsed.y === 'number' &&
      typeof parsed.width === 'number' &&
      typeof parsed.height === 'number'
    ) {
      return { height: parsed.height, width: parsed.width, x: parsed.x, y: parsed.y }
    }
  } catch {
    // fall through to null
  }

  return null
}

function saveBounds(bounds: PetOverlayBounds): void {
  persistString(OVERLAY_BOUNDS_KEY, JSON.stringify(bounds))
}

// The overlay window is padded around the sprite so the bubble (above), the
// drag area, and the pop-up composer all have room; the pet sits near the
// bottom and the rest of the rectangle is transparent + click-through.
const OVERLAY_PAD_X = 100
const OVERLAY_PAD_Y = 200
const OVERLAY_MIN_W = 240
const OVERLAY_MIN_H = 300

/**
 * Window bounds (width/height) that fully contain the pet at a given scale, plus
 * the padding for its bubble/composer/drag margins. The single source of truth
 * for both the initial pop-out size and the live wheel-to-scale resize, so the
 * sprite is never cropped by the window edge no matter how big it's scaled.
 */
export function overlayWindowSize(frameW: number, frameH: number, scale: number): { width: number; height: number } {
  return {
    width: Math.max(OVERLAY_MIN_W, Math.round(frameW * scale + OVERLAY_PAD_X)),
    height: Math.max(OVERLAY_MIN_H, Math.round(frameH * scale + OVERLAY_PAD_Y))
  }
}

let stateUnsubs: Array<() => void> = []
let controlUnsub: (() => void) | null = null
let submitHandler: ((profile: string, text: string) => void) | null = null
let openAppHandler: ((profile: string) => void) | null = null
let scaleHandler: ((profile: string, scale: number) => void) | null = null

// The profiles whose pets this controller has popped out, keyed by normalized
// profile (one overlay per profile, so the key is unique). Replaces a single
// module-level `overlayProfile` so a second concurrent pop-out can't clobber the
// first's push/close target. Main addresses each overlay window by profile and
// stamps the profile onto control payloads (profileForSender); this set is the
// renderer-side state-push target and the fallback when a control payload arrives
// without one. The mirrored payload is the active profile's (follow-active);
// per-overlay state for concurrent pinned overlays is a PR4 concern.
const overlayProfiles = new Map<string, string>()

function currentPayload(): PetOverlayStatePayload {
  return {
    info: $petInfo.get(),
    activity: $petActivity.get(),
    busy: $busy.get(),
    awaiting: $awaitingResponse.get(),
    unread: $petUnread.get(),
    replyText: $petReplyText.get(),
    reaction: $petReaction.get(),
    connection: $profilePets.get().get(normalizeProfileKey(petProfile()))?.connection ?? 'open'
  }
}

function pushNow(): void {
  const api = window.hermesDesktop?.petOverlay

  if (!api || overlayProfiles.size === 0) {
    return
  }

  const payload = currentPayload()

  for (const key of overlayProfiles.keys()) {
    api.pushState(key, payload)
  }
}

/**
 * Open the overlay window and start mirroring live state into it. The main
 * process echoes back the actual screen bounds it used, which we persist so the
 * pet reopens exactly where the user left it.
 */
function openOverlay(profile: string, request: PetOverlayOpenRequest): void {
  const api = window.hermesDesktop?.petOverlay

  if (!api || stateUnsubs.length) {
    return
  }

  const key = normalizeProfileKey(profile)
  overlayProfiles.set(key, key)
  $petOverlayActive.set(true)
  void api.open(key, request).then(res => {
    if (res?.bounds) {
      saveBounds(res.bounds)
    }

    pushNow()
  })

  // Mirror live state into the overlay. subscribe() fires immediately, so the
  // overlay also gets a first frame the moment it's ready (it asks via 'ready').
  stateUnsubs = [
    $petInfo.subscribe(pushNow),
    $petActivity.subscribe(pushNow),
    $busy.subscribe(pushNow),
    $awaitingResponse.subscribe(pushNow),
    $petUnread.subscribe(pushNow),
    $petReplyText.subscribe(pushNow),
    $petReaction.subscribe(pushNow),
    $profilePets.subscribe(pushNow)
  ]
}

/**
 * Pop the pet out of the window. `petRect` is the in-window sprite's viewport
 * rect; we grow it to the padded overlay size and center the window on the
 * pet's old spot (main.ts adds the window's screen origin). If the user has
 * popped out before, reopen at that remembered desktop spot instead.
 */
export function popOutPet(petRect: PetOverlayBounds): void {
  if ($petOverlayActive.get() || stateUnsubs.length) {
    return
  }

  const saved = loadSavedBounds()

  if (saved) {
    openOverlay(petProfile(), { bounds: saved, screen: true })

    return
  }

  // Size the window off the pet's scale (not the measured rect, which includes
  // the shadow) so it matches the live resize math exactly — no jump on open.
  const pet = $petInfo.get()
  const { width, height } = overlayWindowSize(pet.frameW ?? 192, pet.frameH ?? 208, pet.scale ?? 0.33)
  const x = Math.round(petRect.x - (width - petRect.width) / 2)
  const y = Math.round(petRect.y - (height - petRect.height) / 2)

  openOverlay(petProfile(), { bounds: { height, width, x, y }, screen: false })
}

/**
 * Restore the overlay on boot if the pet was popped out when the app last
 * closed. Requires a remembered desktop spot — without one we fall back to the
 * in-window pet rather than spawning an orphan window at the origin.
 */
export function restorePetOverlay(): void {
  if (!window.hermesDesktop?.petOverlay || !$petOverlayActive.get() || stateUnsubs.length) {
    return
  }

  const saved = loadSavedBounds()

  if (!saved) {
    $petOverlayActive.set(false)

    return
  }

  openOverlay(petProfile(), { bounds: saved, screen: true })
}

/**
 * Pop a pet back into the window (closes its overlay window). Without a profile,
 * closes the only/oldest tracked overlay (the follow-active single-pet path).
 * The shared state mirror tears down only when the last overlay closes.
 */
export function popInPet(profile?: string): void {
  const key = profile ? normalizeProfileKey(profile) : overlayProfiles.keys().next().value

  if (key) {
    overlayProfiles.delete(key)
    void window.hermesDesktop?.petOverlay?.close(key)
  }

  if (overlayProfiles.size === 0) {
    for (const off of stateUnsubs) {
      off()
    }

    stateUnsubs = []
    $petOverlayActive.set(false)
  }
}

/** Register the handler that turns an overlay composer submit into a real send.
 *  The profile is sender-derived by main (never trusted from the renderer). */
export function setPetOverlaySubmitHandler(fn: ((profile: string, text: string) => void) | null): void {
  submitHandler = fn
}

/** Register the handler that opens the app to the most recent thread (mail icon). */
export function setPetOverlayOpenAppHandler(fn: ((profile: string) => void) | null): void {
  openAppHandler = fn
}

/** Register the handler that persists a scale resized via the overlay's Alt+wheel gesture. */
export function setPetOverlayScaleHandler(fn: ((profile: string, scale: number) => void) | null): void {
  scaleHandler = fn
}

/**
 * Wire the overlay→renderer control channel once. Returns a disposer. Idempotent
 * — a second call while already wired is a no-op.
 */
export function initPetOverlayBridge(): () => void {
  const api = window.hermesDesktop?.petOverlay

  if (!api || controlUnsub) {
    return () => {}
  }

  controlUnsub = api.onControl(payload => {
    // main derives the profile from the overlay's webContents.id and attaches it
    // before forwarding; fall back to the (single) profile this controller popped
    // out when a payload arrives without one.
    const profile = payload?.profile ?? overlayProfiles.keys().next().value ?? 'default'

    if (payload?.type === 'pop-in') {
      popInPet(profile)
    } else if (payload?.type === 'ready') {
      // The overlay just mounted — hand it the current frame.
      pushNow()
    } else if (payload?.type === 'submit' && typeof payload.text === 'string') {
      submitHandler?.(profile, payload.text)
    } else if (payload?.type === 'bounds' && payload.bounds) {
      // The user dragged the overlay to a new desktop spot — remember it.
      saveBounds(payload.bounds)
    } else if (payload?.type === 'scale' && typeof payload.scale === 'number') {
      // The user resized the popped-out pet (Alt+wheel) — persist it through
      // the main renderer's gateway; the new scale rides $petInfo back to the
      // overlay on the next push, keeping both surfaces in sync.
      scaleHandler?.(profile, payload.scale)
    } else if (payload?.type === 'open-app') {
      // Mail icon / reply bubble: surface the app on the most recent thread
      // (main.ts already focused the window before forwarding this) and mark
      // it read.
      clearPetUnread()
      clearPetReplyText()
      openAppHandler?.(profile)
    }
  })

  return () => {
    controlUnsub?.()
    controlUnsub = null
  }
}
