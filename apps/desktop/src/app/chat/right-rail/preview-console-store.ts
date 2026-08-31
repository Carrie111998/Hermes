/**
 * Per-tab preview console stores.
 *
 * The console panel lives in the pane and its toggle lives in the pane's
 * browser bar, but the store has to outlive any one render and be addressable
 * by tab id — a tab can be parked and remounted, and its logs come back with
 * it. Created lazily per tab and cached here.
 */

import { createPreviewConsoleState, type PreviewConsoleState } from './preview-console-state'

const consoleStates = new Map<string, PreviewConsoleState>()

/** The console store for a tab, created on first use. */
export function previewConsoleState(tabId: string): PreviewConsoleState {
  const existing = consoleStates.get(tabId)

  if (existing) {
    return existing
  }

  const created = createPreviewConsoleState()

  consoleStates.set(tabId, created)

  return created
}

/** The console for a tab if one exists, WITHOUT creating it. The digest asks
 *  about whatever tab the agent resolved to — which can be a file or artifact
 *  tab, or the fallback active tab — and a query that allocated on every miss
 *  would leave an empty store behind for each one. */
export function existingPreviewConsole(tabId: string): PreviewConsoleState | undefined {
  return consoleStates.get(tabId)
}

/** Drop a closed tab's console so a long-lived window doesn't pin every preview
 *  it ever opened. */
export function forgetPreviewConsole(tabId: string) {
  consoleStates.delete(tabId)
}
