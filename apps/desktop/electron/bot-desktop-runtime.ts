import path from 'node:path'
import { pathToFileURL } from 'node:url'

/**
 * Pure identity helpers for Bot Desktop.
 *
 * A Bot Desktop is deliberately scoped by the Hermes profile key. The browser
 * identity, workspace, and viewer window all use this same key so opening two
 * Bots cannot silently reuse the same desktop state.
 */

export const BOT_DESKTOP_WINDOW_FLAG = 'bot-desktop'
export const BOT_DESKTOP_PROFILE_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/

export type BotDesktopProvider = 'docker' | 'wsl'

export interface BotDesktopWindowLike {
  close?: () => void
  focus?: () => void
  isDestroyed?: () => boolean
  isMinimized?: () => boolean
  isVisible?: () => boolean
  on?: (event: string, listener: () => void) => unknown
  restore?: () => void
  show?: () => void
}

export function normalizeBotDesktopProfile(raw: unknown): string {
  const profile = String(raw ?? '').trim()

  if (!profile) {
    return 'default'
  }

  if (!BOT_DESKTOP_PROFILE_RE.test(profile)) {
    throw new Error('Bot Desktop profile must match [a-z0-9][a-z0-9_-]{0,63}.')
  }

  return profile
}

/** Persistent files owned by one Bot Desktop. */
export function botDesktopWorkspacePath(hermesHome: string, profile: unknown): string {
  const root = String(hermesHome || '').trim()

  if (!root) {
    throw new Error('Hermes home is required to resolve a Bot Desktop workspace.')
  }

  const key = normalizeBotDesktopProfile(profile)
  const profileRoot = key === 'default' ? path.resolve(root) : path.join(path.resolve(root), 'profiles', key)

  return path.join(profileRoot, 'desktop-workspace')
}

export function buildBotDesktopWindowUrl(
  profile: unknown,
  { devServer, rendererIndexPath }: { devServer?: string; rendererIndexPath?: string } = {}
): string {
  const params = new URLSearchParams({
    profile: normalizeBotDesktopProfile(profile),
    win: BOT_DESKTOP_WINDOW_FLAG
  })

  if (devServer) {
    const base = devServer.endsWith('/') ? devServer.slice(0, -1) : devServer

    return `${base}/?${params.toString()}`
  }

  if (!rendererIndexPath) {
    throw new Error('A renderer index path is required outside the development server.')
  }

  return `${pathToFileURL(rendererIndexPath).toString()}?${params.toString()}`
}

/** One standalone window per Bot profile, with deterministic focus/reuse. */
export function createBotDesktopWindowRegistry() {
  const windows = new Map<string, BotDesktopWindowLike>()

  function focus(window: BotDesktopWindowLike) {
    if (window.isMinimized?.()) {
      window.restore?.()
    }

    if (window.isVisible && !window.isVisible()) {
      window.show?.()
    }

    window.focus?.()
  }

  function openOrFocus(profile: unknown, factory: (profile: string) => BotDesktopWindowLike | null | undefined) {
    const key = normalizeBotDesktopProfile(profile)
    const existing = windows.get(key)

    if (existing && !existing.isDestroyed?.()) {
      focus(existing)

      return existing
    }

    if (existing) {
      windows.delete(key)
    }

    const window = factory(key)

    if (!window) {
      return null
    }

    windows.set(key, window)
    window.on?.('closed', () => {
      if (windows.get(key) === window) {
        windows.delete(key)
      }
    })

    return window
  }

  function close(profile: unknown) {
    const key = normalizeBotDesktopProfile(profile)
    const window = windows.get(key)

    if (!window) {
      return false
    }

    windows.delete(key)

    if (!window.isDestroyed?.()) {
      window.close?.()
    }

    return true
  }

  function closeAll() {
    const current = [...windows.values()]
    windows.clear()

    for (const window of current) {
      if (!window.isDestroyed?.()) {
        window.close?.()
      }
    }
  }

  return {
    close,
    closeAll,
    get: (profile: unknown) => windows.get(normalizeBotDesktopProfile(profile)),
    has: (profile: unknown) => windows.has(normalizeBotDesktopProfile(profile)),
    openOrFocus,
    get size() {
      return windows.size
    }
  }
}
