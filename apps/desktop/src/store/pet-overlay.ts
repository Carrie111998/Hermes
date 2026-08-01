import { atom } from 'nanostores'

import { chatMessageText } from '@/lib/chat-messages'
import { persistBoolean, persistString, storedBoolean, storedString } from '@/lib/storage'
import {
  $avatarPacks,
  $avatarPreviewState,
  $avatarRendererType,
  $resolvedAvatarPacks,
  $selectedAvatarPack,
  setAvatarPreviewState,
  setAvatarRendererType,
  setSelectedAvatarPackId
} from '@/store/avatar-pack-store'
import type { AvatarPackListResult, AvatarRendererType, AvatarState, ResolvedAvatarPack } from '@/store/avatar-pack-types'
import { $petActivity, $petInfo, $petUnread, clearPetUnread, type PetActivity, type PetInfo } from '@/store/pet'
import { $activeSessionId, $awaitingResponse, $busy, $gatewayState, $messages } from '@/store/session'

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
  /** Latest reaction — bumping its id forwards a burst to the overlay. */
  reaction: PetReaction | null
  /** Avatar display preferences (P0) — pushed so the overlay applies them. */
  avatarSize?: AvatarSizePreset
  avatarOpacity?: AvatarOpacityPreset
  voiceReplies?: boolean
  hidden?: boolean
  /** P0.1: avatar placement mode — desktop overlay vs in-window docked. */
  avatarMode?: AvatarMode
  /** Gateway/session status for the settings panel (P0 read-only display). */
  gatewayStatus?: string
  activeSessionId?: string | null
  /** P1: Avatar pack renderer state — pushed so the overlay renders the right pack. */
  avatarRendererType?: AvatarRendererType
  selectedAvatarPack?: ResolvedAvatarPack | null
  avatarPreviewState?: AvatarState | null
  avatarPackList?: AvatarPackListResult | null
  /** P1.5: The text of the last assistant message, for TTS playback in the overlay. */
  lastAssistantText?: string | null
  /** P1.5: The message id of the last assistant message — monotonically increasing
   *  nonce for duplicate TTS guard in the overlay voice loop. */
  lastAssistantMsgId?: string | null
}

export type PetOverlayControl =
  | { type: 'pop-in' }
  | { type: 'ready' }
  | { type: 'submit'; text: string }
  | { type: 'bounds'; bounds: PetOverlayBounds }
  | { type: 'open-app' }
  | { type: 'toggle-app' }
  | { type: 'scale'; scale: number }
  | { type: 'hide' }
  | { type: 'quit' }
  | { type: 'open-chat' }
  | { type: 'open-settings' }
  | { type: 'dock' }
  | { type: 'pop-out' }
  | { type: 'set-size'; size: AvatarSizePreset }
  | { type: 'set-renderer-type'; rendererType: AvatarRendererType }
  | { type: 'set-pack'; packId: string | null }
  | { type: 'set-preview-state'; state: AvatarState | null }
  | { type: 'reload-packs' }
  | { type: 'open-packs-folder' }
  | { type: 'set-voice-replies'; enabled: boolean }

// ── Avatar size presets (P0) ─────────────────────────────────────────────────
// Maps a named size to a sprite scale. The overlay window resizes to fit.
export type AvatarSizePreset = 'mini' | 'very-small' | 'small' | 'medium' | 'large'

// P0.4: Scales widened dramatically for ~5x more visible size differences.
// Old range was 0.18→0.66 (3.67x ratio); new range is 0.12→1.30 (10.8x ratio).
// The old presets barely changed the window size (all clamped to 240×300 except
// large at 240×337). With the new range, medium/large produce visibly larger
// windows (medium: 244×356, large: 350×471) — the size menu selection is now
// instantly obvious. overlayWindowSize() clamps to 65% of the display work area
// so even extreme scales can't cover the screen.
export const AVATAR_SIZE_SCALES: Record<AvatarSizePreset, number> = {
  mini: 0.12,
  'very-small': 0.25,
  small: 0.40,
  medium: 0.75,
  large: 1.30
}

export const AVATAR_SIZE_LABELS: Record<AvatarSizePreset, string> = {
  mini: 'Mini',
  'very-small': 'Very small',
  small: 'Small',
  medium: 'Medium',
  large: 'Large'
}

// ── Avatar opacity presets (P0) ──────────────────────────────────────────────
export type AvatarOpacityPreset = 'solid' | 'soft' | 'ghost'

// ── Avatar placement mode (P0.1) ────────────────────────────────────────────
// 'desktop' = always-on-top OS-level overlay (default); 'docked' = in-window pet.
export type AvatarMode = 'desktop' | 'docked'

export const AVATAR_MODE_LABELS: Record<AvatarMode, string> = {
  desktop: 'Desktop overlay',
  docked: 'Docked in Hermes'
}

export const AVATAR_OPACITY_VALUES: Record<AvatarOpacityPreset, number> = {
  solid: 0.95,
  soft: 0.7,
  ghost: 0.4
}

export const AVATAR_OPACITY_LABELS: Record<AvatarOpacityPreset, string> = {
  solid: 'Solid',
  soft: 'Soft',
  ghost: 'Ghost'
}

// Persisted across restarts: was the pet popped out, and where on the desktop
// did the user leave it. Keyed v1; bump if the bounds shape ever changes.
const OVERLAY_ACTIVE_KEY = 'hermes.desktop.pet-overlay-active.v1'
const OVERLAY_BOUNDS_KEY = 'hermes.desktop.pet-overlay-bounds.v1'
const OVERLAY_SIZE_KEY = 'hermes.desktop.avatar-size.v1'
const OVERLAY_OPACITY_KEY = 'hermes.desktop.avatar-opacity.v1'
const OVERLAY_VOICE_KEY = 'hermes.desktop.avatar-voice-replies.v1'
const OVERLAY_HIDDEN_KEY = 'hermes.desktop.avatar-hidden.v1'
const OVERLAY_MODE_KEY = 'hermes.desktop.avatar-mode.v1'

export const $petOverlayActive = atom(storedBoolean(OVERLAY_ACTIVE_KEY, false))

// Persist the in/out choice so a popped-out pet comes back popped out.
$petOverlayActive.subscribe(active => persistBoolean(OVERLAY_ACTIVE_KEY, active))

// ── Avatar display preferences (persisted, P0) ───────────────────────────────
// Default: small + solid (Cenk decision 2026-07-28 — not huge, not ghost).
export const $avatarSize = atom<AvatarSizePreset>(
  ((): AvatarSizePreset => {
    const v = storedString(OVERLAY_SIZE_KEY)

    return (v === 'mini' || v === 'very-small' || v === 'small' || v === 'medium' || v === 'large') ? v : 'small'
  })()
)
export const $avatarOpacity = atom<AvatarOpacityPreset>(
  ((): AvatarOpacityPreset => {
    const v = storedString(OVERLAY_OPACITY_KEY)

    return (v === 'solid' || v === 'soft' || v === 'ghost') ? v : 'solid'
  })()
)
export const $avatarVoiceReplies = atom(storedBoolean(OVERLAY_VOICE_KEY, false))
export const $avatarHidden = atom(storedBoolean(OVERLAY_HIDDEN_KEY, false))

// P0.1: Default 'desktop' — avatar auto-pops to OS overlay when a pet activates.
const initialAvatarMode = (): AvatarMode => {
  const v = storedString(OVERLAY_MODE_KEY)

  return v === 'desktop' || v === 'docked' ? v : 'desktop'
}

export const $avatarMode = atom<AvatarMode>(initialAvatarMode())

$avatarSize.subscribe(v => persistString(OVERLAY_SIZE_KEY, v))
$avatarOpacity.subscribe(v => persistString(OVERLAY_OPACITY_KEY, v))
$avatarVoiceReplies.subscribe(v => persistBoolean(OVERLAY_VOICE_KEY, v))
$avatarHidden.subscribe(v => persistBoolean(OVERLAY_HIDDEN_KEY, v))
$avatarMode.subscribe(v => persistString(OVERLAY_MODE_KEY, v))

export function setAvatarSize(v: AvatarSizePreset): void { $avatarSize.set(v) }

export function setAvatarOpacity(v: AvatarOpacityPreset): void { $avatarOpacity.set(v) }

export function setAvatarVoiceReplies(v: boolean): void { $avatarVoiceReplies.set(v) }

export function setAvatarHidden(v: boolean): void { $avatarHidden.set(v) }

export function setAvatarMode(v: AvatarMode): void { $avatarMode.set(v) }

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
      const bounds = { height: parsed.height, width: parsed.width, x: parsed.x, y: parsed.y }

      // Off-screen guard: if the saved bounds are completely outside the
      // current viewport's available area, discard them so we fall back to the
      // default position. The renderer only knows about its own display
      // (window.screen), but that covers the common case of a saved spot on a
      // now-disconnected external monitor. The main process has a more thorough
      // check via screen.getAllDisplays().
      const sw = window.screen.availWidth || 1920
      const sh = window.screen.availHeight || 1080

      if (bounds.x + bounds.width < 0 || bounds.y + bounds.height < 0 || bounds.x > sw || bounds.y > sh) {
        // Bounds are fully off the current display — clear stale value.
        persistString(OVERLAY_BOUNDS_KEY, null)

        return null
      }

      return bounds
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

// P0.4: Maximum fraction of the display work area the overlay window may occupy.
// Prevents large size presets from covering the entire screen.
const OVERLAY_MAX_DISPLAY_FRAC = 0.65

/**
 * Window bounds (width/height) that fully contain the pet at a given scale, plus
 * the padding for its bubble/composer/drag margins. The single source of truth
 * for both the initial pop-out size and the live wheel-to-scale resize, so the
 * sprite is never cropped by the window edge no matter how big it's scaled.
 *
 * P0.4: Clamps the computed width/height to a fraction of the available display
 * area (65% of work-area dimensions). This lets large size presets deliver a
 * dramatic visual difference without covering the entire screen.
 */
export function overlayWindowSize(frameW: number, frameH: number, scale: number): { width: number; height: number } {
  // Clamp the scale itself first — never larger than the display allows.
  const availW = (typeof window !== 'undefined' && window.screen?.availWidth) || 1920
  const availH = (typeof window !== 'undefined' && window.screen?.availHeight) || 1080
  const maxW = availW * OVERLAY_MAX_DISPLAY_FRAC
  const maxH = availH * OVERLAY_MAX_DISPLAY_FRAC

  let width = Math.max(OVERLAY_MIN_W, Math.round(frameW * scale + OVERLAY_PAD_X))
  let height = Math.max(OVERLAY_MIN_H, Math.round(frameH * scale + OVERLAY_PAD_Y))

  // Clamp to max fraction of the display work area.
  width = Math.min(width, Math.round(maxW))
  height = Math.min(height, Math.round(maxH))

  return { width, height }
}

let stateUnsubs: Array<() => void> = []
let controlUnsub: (() => void) | null = null
let submitHandler: ((text: string) => void) | null = null
let openAppHandler: (() => void) | null = null
let scaleHandler: ((scale: number) => void) | null = null
let hideHandler: (() => void) | null = null
let quitHandler: (() => void) | null = null
let openChatHandler: (() => void) | null = null
let openSettingsHandler: (() => void) | null = null
let dockHandler: (() => void) | null = null
let popOutHandler: (() => void) | null = null
let setSizeHandler: ((size: AvatarSizePreset) => void) | null = null
let setVoiceRepliesHandler: ((enabled: boolean) => void) | null = null

/**
 * Walk the live message log backwards and return the most recent assistant
 * message's text + id. Returns null text when no assistant message exists
 * yet, or every assistant message is whitespace-only (e.g. tool-only turn).
 *
 * SELECTIVE ADOPT note (2026-07-28): This is the renderer's mirror of what
 * the third-party repo's `state_machine.py` produces server-side. We don't
 * ship a separate state machine — the agent's session store IS the source
 * of truth, and the overlay just tails it. That keeps the desktop and the
 * overlay in lockstep without a second opinion.
 *
 * P1.5: The returned `id` is forwarded as `lastAssistantMsgId` so the
 * overlay's voice loop can deduplicate TTS across re-pushes.
 */
function getLastAssistantText(): { text: string | null; id: string | null } {
  const messages = $messages.get()

  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i]

    if (msg.role === 'assistant') {
      const text = chatMessageText(msg)

      if (text.trim()) {
        return { text: text.trim(), id: msg.id || null }
      }
    }
  }

  return { text: null, id: null }
}

function currentPayload(): PetOverlayStatePayload {
  const { text, id } = getLastAssistantText()

  return {
    info: $petInfo.get(),
    activity: $petActivity.get(),
    busy: $busy.get(),
    awaiting: $awaitingResponse.get(),
    unread: $petUnread.get(),
    reaction: $petReaction.get(),
    avatarSize: $avatarSize.get(),
    avatarOpacity: $avatarOpacity.get(),
    voiceReplies: $avatarVoiceReplies.get(),
    hidden: $avatarHidden.get(),
    avatarMode: $avatarMode.get(),
    gatewayStatus: $gatewayState.get(),
    activeSessionId: $activeSessionId.get(),
    // P1: Avatar pack renderer state
    avatarRendererType: $avatarRendererType.get(),
    selectedAvatarPack: $selectedAvatarPack.get(),
    avatarPreviewState: $avatarPreviewState.get(),
    avatarPackList: $avatarPacks.get(),
    // P1.5: Last assistant text + message id for overlay TTS.
    lastAssistantText: text,
    lastAssistantMsgId: id
  }
}

function pushNow(): void {
  window.hermesDesktop?.petOverlay?.pushState(currentPayload())
}

/**
 * Open the overlay window and start mirroring live state into it. The main
 * process echoes back the actual screen bounds it used, which we persist so the
 * pet reopens exactly where the user left it.
 */
function openOverlay(request: PetOverlayOpenRequest): void {
  const api = window.hermesDesktop?.petOverlay

  if (!api || stateUnsubs.length) {
    // Diagnostic: explain why we bailed (helps debug "overlay not visible").
    console.warn('[pet-overlay] openOverlay skipped:', !api ? 'no IPC bridge' : 'already active')

    return
  }

  $petOverlayActive.set(true)
  void api.open(request).then(res => {
    if (res?.bounds) {
      saveBounds(res.bounds)
    }

    pushNow()

    // Retry push: the overlay's React tree may not have mounted when the first
    // pushNow() fires (it loads via ?win=overlay in a separate BrowserWindow).
    // The overlay signals readiness via the 'ready' control message, but that
    // can race with our subscribe() calls below. A delayed retry ensures the
    // first frame arrives even if 'ready' was missed or lost.
    setTimeout(() => pushNow(), 500)
    setTimeout(() => pushNow(), 1500)
  }).catch(err => {
    console.error('[pet-overlay] open IPC failed:', err)
    $petOverlayActive.set(false)
  })

  // Mirror live state into the overlay. subscribe() fires immediately, so the
  // overlay also gets a first frame the moment it's ready (it asks via 'ready').
  stateUnsubs = [
    $petInfo.subscribe(pushNow),
    $petActivity.subscribe(pushNow),
    $busy.subscribe(pushNow),
    $awaitingResponse.subscribe(pushNow),
    $petUnread.subscribe(pushNow),
    $petReaction.subscribe(pushNow),
    $avatarSize.subscribe(pushNow),
    $avatarOpacity.subscribe(pushNow),
    $avatarVoiceReplies.subscribe(pushNow),
    $avatarHidden.subscribe(pushNow),
    $avatarMode.subscribe(pushNow),
    $gatewayState.subscribe(pushNow),
    $activeSessionId.subscribe(pushNow),
    // P1: Avatar pack renderer state
    $avatarRendererType.subscribe(pushNow),
    $selectedAvatarPack.subscribe(pushNow),
    $avatarPreviewState.subscribe(pushNow),
    $avatarPacks.subscribe(pushNow)
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
    openOverlay({ bounds: saved, screen: true })

    return
  }

  // Size the window off the pet's scale (not the measured rect, which includes
  // the shadow) so it matches the live resize math exactly — no jump on open.
  const pet = $petInfo.get()
  const { width, height } = overlayWindowSize(pet.frameW ?? 192, pet.frameH ?? 208, pet.scale ?? 0.33)
  const x = Math.round(petRect.x - (width - petRect.width) / 2)
  const y = Math.round(petRect.y - (height - petRect.height) / 2)

  openOverlay({ bounds: { height, width, x, y }, screen: false })
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

  openOverlay({ bounds: saved, screen: true })
}

/**
 * P0.1: Auto-pop the pet to the desktop overlay. Called when a pet becomes
 * active (info.enabled + spritesheet loaded) and avatarMode is 'desktop'.
 *
 * If the user previously dragged the overlay to a spot, reopen there.
 * Otherwise, open at a sensible default: bottom-right of the primary display.
 */
export function autoPopOutPet(): void {
  if (!window.hermesDesktop?.petOverlay || $petOverlayActive.get() || stateUnsubs.length) {
    console.warn('[pet-overlay] autoPopOutPet skipped:', !window.hermesDesktop?.petOverlay ? 'no IPC bridge' : $petOverlayActive.get() ? 'already active' : 'subs exist')

    return
  }

  const saved = loadSavedBounds()

  if (saved) {
    console.info('[pet-overlay] autoPopOutPet: using saved bounds', saved)
    openOverlay({ bounds: saved, screen: true })

    return
  }

  // Default position: bottom-right of the work area, leaving a margin.
  // Use screen dimensions (available in the renderer via window.screen).
  const sw = window.screen.availWidth || 1440
  const sh = window.screen.availHeight || 900
  const pet = $petInfo.get()
  const { width, height } = overlayWindowSize(pet.frameW ?? 192, pet.frameH ?? 208, pet.scale ?? 0.33)

  const bounds = {
    x: Math.round(sw - width - 24),
    y: Math.round(sh - height - 24),
    width,
    height
  }

  console.info('[pet-overlay] autoPopOutPet: using default bounds', bounds)
  openOverlay({ bounds, screen: true })
}

/**
 * P0.1: Dock the avatar back into the Hermes window (close the overlay).
 * Sets avatarMode to 'docked' and pops the pet in.
 */
export function dockAvatar(): void {
  $avatarMode.set('docked')

  if ($petOverlayActive.get()) {
    popInPet()
  }
}

/**
 * P0.1: Pop the avatar out to the desktop overlay.
 * Sets avatarMode to 'desktop' and auto-pops the overlay.
 */
export function undockAvatar(): void {
  $avatarMode.set('desktop')

  if (!$petOverlayActive.get()) {
    autoPopOutPet()
  }
}

/** Pop the pet back into the window (closes the overlay window). */
export function popInPet(): void {
  for (const off of stateUnsubs) {
    off()
  }

  stateUnsubs = []
  $petOverlayActive.set(false)
  void window.hermesDesktop?.petOverlay?.close()
}

/** Register the handler that turns an overlay composer submit into a real send. */
export function setPetOverlaySubmitHandler(fn: ((text: string) => void) | null): void {
  submitHandler = fn
}

/** Register the handler that opens the app to the most recent thread (mail icon). */
export function setPetOverlayOpenAppHandler(fn: (() => void) | null): void {
  openAppHandler = fn
}

/** Register the handler that persists a scale resized via the overlay's Alt+wheel gesture. */
export function setPetOverlayScaleHandler(fn: ((scale: number) => void) | null): void {
  scaleHandler = fn
}

/** Register handler invoked when the user picks "Hide avatar" from the context menu. */
export function setPetOverlayHideHandler(fn: (() => void) | null): void {
  hideHandler = fn
}

/** Register handler invoked when the user picks "Quit" from the context menu. */
export function setPetOverlayQuitHandler(fn: (() => void) | null): void {
  quitHandler = fn
}

/** Register handler invoked when the user picks "Chat…" or double-clicks the avatar. */
export function setPetOverlayOpenChatHandler(fn: (() => void) | null): void {
  openChatHandler = fn
}

/** Register handler invoked when the user picks "Settings…" from the context menu. */
export function setPetOverlayOpenSettingsHandler(fn: (() => void) | null): void {
  openSettingsHandler = fn
}

/** P0.1: Register handler invoked when the user picks "Dock to Hermes window". */
export function setPetOverlayDockHandler(fn: (() => void) | null): void {
  dockHandler = fn
}

/** P0.1: Register handler invoked when the user picks "Pop out to desktop". */
export function setPetOverlayPopOutHandler(fn: (() => void) | null): void {
  popOutHandler = fn
}

/**
 * P0.3: Register handler invoked when the overlay's size preset changes.
 * The main renderer persists the new size to $avatarSize (which triggers
 * pushNow via the subscription), keeping both surfaces in sync.
 */
export function setPetOverlaySetSizeHandler(fn: ((size: AvatarSizePreset) => void) | null): void {
  setSizeHandler = fn
}

/** P0.5: Register handler invoked when the overlay toggles voice replies. */
export function setPetOverlaySetVoiceRepliesHandler(fn: ((enabled: boolean) => void) | null): void {
  setVoiceRepliesHandler = fn
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
    if (payload?.type === 'pop-in') {
      popInPet()
    } else if (payload?.type === 'ready') {
      // The overlay just mounted — hand it the current frame.
      pushNow()
    } else if (payload?.type === 'submit' && typeof payload.text === 'string') {
      submitHandler?.(payload.text)
    } else if (payload?.type === 'bounds' && payload.bounds) {
      // The user dragged the overlay to a new desktop spot — remember it.
      saveBounds(payload.bounds)
    } else if (payload?.type === 'scale' && typeof payload.scale === 'number') {
      // The user resized the popped-out pet (Alt+wheel) — persist it through
      // the main renderer's gateway; the new scale rides $petInfo back to the
      // overlay on the next push, keeping both surfaces in sync.
      scaleHandler?.(payload.scale)
    } else if (payload?.type === 'open-app') {
      // Mail icon: surface the app on the most recent thread (main.ts already
      // focused the window before forwarding this) and mark it read.
      clearPetUnread()
      openAppHandler?.()
    } else if (payload?.type === 'hide') {
      hideHandler?.()
    } else if (payload?.type === 'quit') {
      quitHandler?.()
    } else if (payload?.type === 'open-chat') {
      openChatHandler?.()
    } else if (payload?.type === 'open-settings') {
      openSettingsHandler?.()
    } else if (payload?.type === 'dock') {
      dockHandler?.()
    } else if (payload?.type === 'pop-out') {
      popOutHandler?.()
    } else if (payload?.type === 'set-size' && payload.size) {
      // P0.3: Overlay picked a new size preset — persist via $avatarSize so
      // the subscription fires pushNow with the updated size, keeping both
      // surfaces in sync. This replaces the old handleSizeChange path that
      // set $petInfo.scale directly and caused the main renderer to push
      // back the OLD avatarSize, reverting the overlay's local state.
      setSizeHandler?.(payload.size)
    } else if (payload?.type === 'set-renderer-type' && payload.rendererType) {
      setAvatarRendererType(payload.rendererType)
    } else if (payload?.type === 'set-pack') {
      setSelectedAvatarPackId(payload.packId)
    } else if (payload?.type === 'set-preview-state') {
      setAvatarPreviewState(payload.state)
    } else if (payload?.type === 'reload-packs') {
      reloadAvatarPacks()
    } else if (payload?.type === 'set-voice-replies' && typeof payload.enabled === 'boolean') {
      // P0.5: Overlay toggled voice replies — persist via $avatarVoiceReplies so
      // the subscription fires pushNow with the updated value, keeping both
      // surfaces in sync and surviving restart.
      setVoiceRepliesHandler?.(payload.enabled)
    } else if (payload?.type === 'open-packs-folder') {
      void window.hermesDesktop?.avatarPacks?.open()
    }
  })

  return () => {
    controlUnsub?.()
    controlUnsub = null
  }
}

/**
 * P1: Reload avatar packs from the filesystem via IPC, updating both the
 * summary list and the resolved packs (with asset URLs).
 */
export async function reloadAvatarPacks(): Promise<void> {
  try {
    const [list, resolved] = await Promise.all([
      window.hermesDesktop?.avatarPacks?.list(),
      window.hermesDesktop?.avatarPacks?.resolve()
    ])

    if (list) {
      $avatarPacks.set(list)
    }

    $resolvedAvatarPacks.set(resolved ?? [])
  } catch (err) {
    console.error('[avatar-packs] reload failed:', err)
  }
}

/**
 * P1: Load avatar packs on initial mount. Called from the main renderer's
 * pet-overlay initialization. Safe to call multiple times.
 */
export async function initAvatarPacks(): Promise<void> {
  await reloadAvatarPacks()
}
