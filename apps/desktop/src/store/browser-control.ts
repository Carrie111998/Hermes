/**
 * BROWSER CONTROL — who is allowed to drive the in-app Browser right now.
 *
 * The pane has always been drivable from two directions: the agent through
 * `drive_preview` (see `right-rail/preview-act.ts`) and the person through the
 * webview itself. Nothing said which of them was doing it, and nothing let the
 * person say "stop, I've got this" — so a hand-off was a race.
 *
 * Two pieces of state, deliberately separate:
 *
 *   - MODE is intent, per Browser tab, and only the person changes it. `agent`
 *     (the default, so Browser Use behaves exactly as before) means Hermes may
 *     act; `manual` means it may not, and an action arriving in manual mode is
 *     refused with an explanation rather than silently dropped.
 *   - ACTIVITY is fact: the tab the agent is acting on at this instant. It is
 *     what the "Hermes is driving" indicator reads, so the badge tracks real
 *     actions instead of a mode that is merely permissive.
 *
 * Memory-only on purpose. A relaunch hands control back to the agent, which is
 * the safe default — a stale `manual` flag from last week would look like the
 * agent silently refusing to work.
 */

import { atom, computed } from 'nanostores'

export type BrowserControlMode = 'agent' | 'manual'

export const BROWSER_CONTROL_DEFAULT: BrowserControlMode = 'agent'

/** Only tabs the user has taken over are recorded; absence means `agent`. */
export const $browserControlModes = atom<Readonly<Record<string, BrowserControlMode>>>({})

/** The Browser tab the agent is acting on right now, if any. */
export const $browserAgentActingTabId = atom<null | string>(null)

export function browserControlMode(tabId: null | string | undefined): BrowserControlMode {
  return (tabId && $browserControlModes.get()[tabId]) || BROWSER_CONTROL_DEFAULT
}

export function setBrowserControlMode(tabId: string, mode: BrowserControlMode) {
  if (!tabId || browserControlMode(tabId) === mode) {
    return
  }

  const next = { ...$browserControlModes.get() }

  if (mode === BROWSER_CONTROL_DEFAULT) {
    delete next[tabId]
  } else {
    next[tabId] = mode
  }

  $browserControlModes.set(next)

  // Handing the wheel over mid-action would leave the badge lit on a tab the
  // agent is no longer allowed to touch.
  if (mode === 'manual' && $browserAgentActingTabId.get() === tabId) {
    $browserAgentActingTabId.set(null)
  }
}

export function toggleBrowserControlMode(tabId: string) {
  setBrowserControlMode(tabId, browserControlMode(tabId) === 'manual' ? 'agent' : 'manual')
}

/** Drop a closed tab's record so a later tab minted with a recycled id can't
 *  inherit it. Ids are random today, but the cleanup is free. */
export function forgetBrowserControl(tabId: string) {
  const current = $browserControlModes.get()

  if (!(tabId in current)) {
    return
  }

  const { [tabId]: gone, ...rest } = current

  void gone
  $browserControlModes.set(rest)

  if ($browserAgentActingTabId.get() === tabId) {
    $browserAgentActingTabId.set(null)
  }
}

/** Mark an agent action as in flight on `tabId`; the disposer clears it. */
export function markBrowserAgentActing(tabId: null | string): () => void {
  if (!tabId) {
    return () => {}
  }

  $browserAgentActingTabId.set(tabId)

  return () => {
    if ($browserAgentActingTabId.get() === tabId) {
      $browserAgentActingTabId.set(null)
    }
  }
}

export const $anyBrowserAgentActing = computed($browserAgentActingTabId, tabId => tabId !== null)
