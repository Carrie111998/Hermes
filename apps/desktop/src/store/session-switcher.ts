import { atom } from 'nanostores'

import type { SessionInfo } from '@/types/hermes'

import { $profileScope, ALL_PROFILES } from './profile'
import { filterSessionsByProfileScope } from './profile-scope'
import { $selectedStoredSessionId, $sessions, sessionMatchesStoredId } from './session'

// Mac-style session switcher (^Tab). Quick tap jumps on keydown; the HUD opens
// only when Tab is held past REVEAL_MS or tapped again while Ctrl is down.

export const SWITCHER_REVEAL_MS = 220

export const $switcherOpen = atom(false)
export const $switcherSessions = atom<SessionInfo[]>([])
export const $switcherIndex = atom(0)

const wrap = (index: number, length: number): number => ((index % length) + length) % length

const switcherCandidates = (): SessionInfo[] => {
  const profileScope = $profileScope.get()

  if (profileScope === ALL_PROFILES) {
    return []
  }

  return filterSessionsByProfileScope($sessions.get(), profileScope)
}

let pendingBrowse = false
let revealTimer: ReturnType<typeof setTimeout> | null = null
let tabHeld = false
let closedAt = 0

function clearRevealTimer(): void {
  if (revealTimer) {
    clearTimeout(revealTimer)
    revealTimer = null
  }
}

function revealOverlay(): void {
  pendingBrowse = false
  $switcherOpen.set(true)
}

function scheduleReveal(): void {
  clearRevealTimer()
  revealTimer = setTimeout(() => {
    revealTimer = null

    if (pendingBrowse && tabHeld) {
      revealOverlay()
    }
  }, SWITCHER_REVEAL_MS)
}

export function onSwitcherTabDown(): void {
  tabHeld = true
}

export function onSwitcherTabUp(): void {
  tabHeld = false

  if (!$switcherOpen.get()) {
    clearRevealTimer()
  }
}

// First Tab returns a session id to jump to immediately; later Tabs move the
// highlight (Ctrl↑ commits when the HUD is open).
export function openOrAdvanceSwitcher(direction: 1 | -1): string | null {
  const sessions = switcherCandidates()

  if (sessions.length < 2) {
    return null
  }

  if ($switcherOpen.get()) {
    const { length } = $switcherSessions.get()

    if (length) {
      $switcherIndex.set(wrap($switcherIndex.get() + direction, length))
    }

    return null
  }

  const selected = $selectedStoredSessionId.get()
  const current = selected === null ? -1 : sessions.findIndex(session => sessionMatchesStoredId(session, selected))
  const start = current === -1 ? (direction === 1 ? -1 : 0) : current
  const nextIndex = wrap(start + direction, sessions.length)

  $switcherSessions.set(sessions)
  $switcherIndex.set(nextIndex)

  if (pendingBrowse) {
    clearRevealTimer()
    $switcherIndex.set(wrap($switcherIndex.get() + direction, sessions.length))
    revealOverlay()

    return null
  }

  pendingBrowse = true
  scheduleReveal()

  return sessions[nextIndex]?.id ?? null
}

export const highlightedSessionId = (): string | null => $switcherSessions.get()[$switcherIndex.get()]?.id ?? null

export const slotSessionId = (slot: number): string | null =>
  ($switcherOpen.get() || pendingBrowse ? $switcherSessions.get() : switcherCandidates())[slot - 1]?.id ?? null

export function closeSwitcher(): void {
  closedAt = Date.now()
  clearRevealTimer()
  pendingBrowse = false
  tabHeld = false
  $switcherOpen.set(false)
}

export function commitOnCtrlUp(): string | null {
  clearRevealTimer()
  pendingBrowse = false

  if (!$switcherOpen.get()) {
    return null
  }

  const target = highlightedSessionId()
  closeSwitcher()

  return target
}

export const switcherJustClosed = (): boolean => Date.now() - closedAt < 400

export const switcherActive = (): boolean => $switcherOpen.get() || pendingBrowse

const unsubscribeProfileScope = $profileScope.listen(() => {
  if (switcherActive()) {
    closeSwitcher()
  }
})

import.meta.hot?.dispose(unsubscribeProfileScope)
