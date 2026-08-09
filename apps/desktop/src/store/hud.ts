/**
 * HUD mode — the chrome-free floating chat.
 *
 * A transparent, frameless, always-on-top window showing nothing but the REAL
 * composer with the reply scrolling above it, so Hermes can be driven while
 * the user works in another app (Figma, a browser).
 *
 * It is NOT a puppet window. Unlike the pet overlay / quick entry, the HUD is
 * a full app renderer with its own gateway — the same thing `openWindow()`
 * spawns, just reshaped. That is the whole design: the HUD renders `ChatView`
 * in its `hud` variant, so the composer IS the app's composer (attachments,
 * slash commands, queue, voice, model pill) rather than a lookalike that
 * drifts. This module owns only the mode flag and the window lifecycle.
 */

import { atom } from 'nanostores'

import { requestComposerDraftSync } from '@/store/composer'
import { $activeGatewayProfile, normalizeProfileKey } from '@/store/profile'
import { $sessions, rememberedSessionProfile } from '@/store/session'
import { isHudWindow } from '@/store/windows'

/** Whether a HUD window is currently up. In the HUD's own renderer this is
 *  always true (it IS the HUD); in the main window it tracks the child so the
 *  titlebar toggle reads correctly.
 *
 *  Deliberately NOT persisted. The HUD is a live window main owns, so it can
 *  never outlive the app — a remembered `true` from the last run just makes the
 *  first toggle a no-op ("the button does nothing after a restart"). Main
 *  broadcasts the truth on every change, which is the only authority there is. */
export const $hudActive = atom(isHudWindow())

/** True only in the HUD window itself — the renderer flag that swaps the app
 *  shell for the slim floating layout. Constant for the window's life, so it
 *  never invalidates a render path mid-session. */
export const $hudMode = atom(isHudWindow())

/** Metadata-only identity of the OS window currently underneath the HUD. The
 * native main process remains authoritative; this atom is the HUD renderer's
 * short-lived cache for its status chip and the next prompt submission. */
export interface HudWindowContext {
  app: string
  title: string
}

export const $hudWindowContext = atom<HudWindowContext | null>(null)

const HUD_WINDOW_CONTEXT_POLL_MS = 1500
const HUD_WINDOW_POSITION_POLL_MS = 250

const cleanWindowContextField = (value: unknown, limit: number): string =>
  typeof value === 'string' ? value.trim().replace(/\s+/g, ' ').slice(0, limit) : ''

/** Reduce the native read result to the privacy boundary HUD mode exposes. */
export function hudWindowContextFromResult(result: unknown): HudWindowContext | null {
  const rawWindow =
    result && typeof result === 'object' ? (result as { window?: { app?: unknown; title?: unknown } | null }).window : null

  if (!rawWindow || typeof rawWindow !== 'object') {
    return null
  }

  const app = cleanWindowContextField(rawWindow.app, 120)
  const title = cleanWindowContextField(rawWindow.title, 240)

  return app || title ? { app, title } : null
}

function publishHudWindowContext(next: HudWindowContext | null): void {
  const current = $hudWindowContext.get()

  if (current?.app === next?.app && current?.title === next?.title) {
    return
  }

  $hudWindowContext.set(next)
}

/**
 * Live-track the window below for as long as the HUD renderer is mounted.
 *
 * A 1.5s metadata poll is the portability fallback. The renderer has no native
 * move event, so a cheap position sampler requests an immediate refresh when
 * the HUD is dragged. Reads are serialized and coalesced: slow enumeration can
 * never stack native work or let an older response overwrite a newer one.
 */
export function startHudWindowContextTracking(): () => void {
  const read = window.hermesDesktop?.readWindowBelow

  publishHudWindowContext(null)

  if (!read) {
    return () => {}
  }

  let alive = true
  let reading = false
  let rerun = false
  let positionKey = `${window.screenX}:${window.screenY}:${window.innerWidth}:${window.innerHeight}`

  const refresh = async () => {
    if (reading) {
      rerun = true

      return
    }

    reading = true

    do {
      rerun = false

      try {
        const result = await read()

        if (alive) {
          publishHudWindowContext(hudWindowContextFromResult(result))
        }
      } catch {
        if (alive) {
          publishHudWindowContext(null)
        }
      }
    } while (alive && rerun)

    reading = false
  }

  const refreshOnMove = () => {
    const next = `${window.screenX}:${window.screenY}:${window.innerWidth}:${window.innerHeight}`

    if (next === positionKey) {
      return
    }

    positionKey = next
    void refresh()
  }

  void refresh()
  const pollTimer = window.setInterval(() => void refresh(), HUD_WINDOW_CONTEXT_POLL_MS)
  const positionTimer = window.setInterval(refreshOnMove, HUD_WINDOW_POSITION_POLL_MS)
  window.addEventListener('focus', refresh)

  return () => {
    alive = false
    window.clearInterval(pollTimer)
    window.clearInterval(positionTimer)
    window.removeEventListener('focus', refresh)
    publishHudWindowContext(null)
  }
}

/** Which conversation the HUD is showing, as far as this window knows. Lets the
 *  toggle tell "switch the HUD to this tab" apart from "dismiss the HUD". */
export const $hudSession = atom<null | string>(null)

/** True when the shell exposes HUD mode (desktop only). */
export const canUseHud = (): boolean =>
  typeof window !== 'undefined' && typeof window.hermesDesktop?.hud?.open === 'function'

export function openHud(sessionId?: null | string): void {
  const api = window.hermesDesktop?.hud

  if (!api) {
    return
  }

  // Push whatever is half-typed here into the shared draft stash BEFORE the
  // HUD window exists, so its composer boots with the text rather than racing
  // a cross-window storage event that lands after it has already painted.
  requestComposerDraftSync('flush')

  // Which backend the HUD must boot against. The HUD is a full renderer that
  // adopts the PRIMARY backend's profile by default, so handing it a session
  // from a non-primary profile without saying so resolves the id against the
  // wrong backend — the lookup misses and the HUD falls back to the default
  // profile's last session (#82285). Same ladder the remembered-navigation key
  // uses: the session's stamped owner wins, and a fresh/unstamped/uncached
  // target inherits the profile the user is looking at.
  const profile = normalizeProfileKey(
    rememberedSessionProfile($sessions.get(), sessionId ?? null, $activeGatewayProfile.get())
  )

  $hudActive.set(true)
  $hudSession.set(sessionId ?? null)
  void api.open({ sessionId: sessionId ?? null, profile })
}

/** Leave HUD mode. Callable from either window — main closes the child, the
 *  HUD closes itself; both restore the app window. */
export function closeHud(): void {
  const api = window.hermesDesktop?.hud

  if (!api) {
    return
  }

  $hudActive.set(false)
  $hudSession.set(null)
  void api.close()
}

export const toggleHud = (sessionId?: null | string) => ($hudActive.get() ? closeHud() : openHud(sessionId))

/** Tell main which session this HUD is on. Main holds it (the HUD's renderer
 *  doesn't outlive the window) and hands it back in the close broadcast so the
 *  app window knows what to re-home onto. */
export const reportHudSession = (sessionId: null | string): void => window.hermesDesktop?.hud?.setSession?.(sessionId)

/**
 * Track the HUD window's real state so the titlebar toggle can't go stale when
 * the HUD is closed from its own side (⌘W, its exit button), and hand the
 * app window the session the HUD ended on. Returns a disposer; no-ops outside
 * Electron.
 */
export function watchHudState(onClosed?: (sessionId: null | string) => void): () => void {
  const off = window.hermesDesktop?.hud?.onChanged?.(({ open, sessionId }) => {
    $hudActive.set(open)
    $hudSession.set(open ? sessionId : null)

    if (!open) {
      onClosed?.(sessionId)
    }
  })

  return off ?? (() => {})
}
