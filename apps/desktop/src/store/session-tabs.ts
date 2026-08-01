/**
 * Session tabs preference — whether the desktop may stack a second chat as a
 * tab instead of loading into main.
 *
 * On by default (today's behavior after #71848). Off is a UX preference for
 * users who never want multi-session tabs: sidebar "+" replaces main, and
 * ⌘/⌃-click + middle-click on a session row load in-place.
 *
 * Presentation-scoped only (desktop AGENTS.md: state lives with its authority).
 * Does not hide the tab strip or block explicit split/⌘T — those remain available
 * when the user deliberately opens them.
 */

import { atom } from 'nanostores'

import { persistBoolean, storedBoolean } from '@/lib/storage'

const KEY = 'hermes.desktop.sessionTabs.v1'

/** Default true — preserve the current "open a tab when main is occupied" behavior. */
export const $sessionTabsEnabled = atom<boolean>(
  typeof window === 'undefined' ? true : storedBoolean(KEY, true)
)

export function setSessionTabsEnabled(enabled: boolean): void {
  $sessionTabsEnabled.set(enabled)
}

/** Sync read for non-React doors (openSession, sidebar "+", openTab gates). */
export function sessionTabsEnabled(): boolean {
  return $sessionTabsEnabled.get()
}

if (typeof window !== 'undefined') {
  $sessionTabsEnabled.listen(enabled => {
    persistBoolean(KEY, enabled)
  })
}
