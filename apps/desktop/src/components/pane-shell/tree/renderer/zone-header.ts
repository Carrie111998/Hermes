/**
 * When a zone's tab strip is hidden.
 *
 * Default: a single pane isn't a "tab", so the header auto-hides; a stack
 * shows its chips. Closeable tiles and collapse tool panels force a lone
 * header so they stay closable.
 *
 * Session-strip zones (the workspace + session tiles) are the chat switcher.
 * Auto-hiding them on a lone workspace — or honoring a persisted
 * `headerHidden` from a double-tap / crash-stale layout — is how the bar
 * vanishes across restarts and client updates. Those zones keep the strip
 * unless a full-page view vetoes it.
 */

export function isSessionStripPaneId(paneId: string): boolean {
  return paneId === 'workspace' || paneId.startsWith('session-tile:')
}

export function sessionStripAllowsHide(shown: readonly string[]): boolean {
  return !shown.some(isSessionStripPaneId)
}

export function resolveZoneHeaderHidden(input: {
  forceLoneHeader: boolean
  headerVeto: boolean
  persistedHidden?: boolean
  shown: readonly string[]
}): boolean {
  if (input.headerVeto) {
    return true
  }

  if (!sessionStripAllowsHide(input.shown)) {
    return false
  }

  return input.persistedHidden ?? (input.shown.length <= 1 && !input.forceLoneHeader)
}
