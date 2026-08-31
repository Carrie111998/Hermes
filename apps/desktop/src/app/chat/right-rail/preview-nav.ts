/**
 * Browser gestures that land on the app's own chrome — ⌘R with the address bar
 * focused, a mouse's back/forward buttons over the pane's frame.
 *
 * The interesting case is handled elsewhere: when the user is INSIDE the guest
 * page, main acts on the focused webContents directly (see
 * `commandFocusedGuest`), because a webview guest is out-of-process and nothing
 * in this renderer can see it. This registry only covers the other half — focus
 * sitting in Hermes' own DOM, where `activeElement` is authoritative.
 */

import { $rightRailActiveTabId } from '@/store/layout'
import { $previewTabs, agentPreviewTabId } from '@/store/preview'

/** Marks a live browser pane so a gesture can find the one holding focus. */
export const PREVIEW_BROWSER_ATTR = 'data-preview-browser'

export interface PreviewNavHandle {
  back: () => void
  forward: () => void
  /** Go to an address in THIS tab. Same path as typing in the address bar, so
   *  the agent gets the same loopback reach and error reporting a person does.
   *  Absent on panes with no address (a remote HTML preview). */
  navigate?: (url: string) => void
  reload: () => void
}

const handles = new Map<string, PreviewNavHandle>()

/** Register a live browser pane's commands; returns an idempotent remove. */
export function registerPreviewNav(tabId: string, handle: PreviewNavHandle): () => void {
  handles.set(tabId, handle)

  return () => {
    if (handles.get(tabId) === handle) {
      handles.delete(tabId)
    }
  }
}

/** The ACTIVE preview tab's commands, for callers with no focus to key off. */
export function activePreviewNav(): PreviewNavHandle | null {
  const tabs = $previewTabs.get()
  const tab = tabs.find(t => t.id === $rightRailActiveTabId.get()) ?? tabs[0]

  return (tab && handles.get(tab.id)) || null
}

/** The commands for the tab the AGENT drives — its own, so `drive_preview`
 *  cannot send the page you are reading back through history, and `sessionId`
 *  so it cannot send another conversation's page there either. */
export function agentPreviewNav(sessionId: null | string): PreviewNavHandle | null {
  const id = agentPreviewTabId(sessionId)

  return (id && handles.get(id)) || null
}

/** Run `command` on the browser pane holding DOM focus. False = focus is
 *  elsewhere in the app, so the caller falls back to the app-level meaning.
 *  Gestures only — `navigate` carries an address and has no keystroke. */
export function commandFocusedPreview(command: 'back' | 'forward' | 'reload'): boolean {
  const host = document.activeElement?.closest(`[${PREVIEW_BROWSER_ATTR}]`)
  const nav = host ? handles.get(host.getAttribute(PREVIEW_BROWSER_ATTR) || '') : undefined

  nav?.[command]()

  return Boolean(nav)
}
