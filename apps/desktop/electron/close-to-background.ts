export interface CloseToBackgroundDecision {
  enabled: boolean
  quitRequested: boolean
  updateHandoff: boolean
}

/**
 * Return true only for a user-initiated main-window close that should hide the
 * window while leaving Electron and its Hermes backend alive.
 *
 * Explicit app quits and updater handoffs must always reach Electron's normal
 * teardown path so processes and files are released cleanly.
 */
export function shouldHideMainWindowOnClose({
  enabled,
  quitRequested,
  updateHandoff
}: CloseToBackgroundDecision): boolean {
  return enabled && !quitRequested && !updateHandoff
}
