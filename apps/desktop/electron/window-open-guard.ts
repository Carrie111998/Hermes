import type { WebContents } from 'electron'

/**
 * Install the fail-closed popup policy used for untrusted guest content.
 *
 * Legitimate Desktop links use the explicit `hermes:openExternal` bridge. A
 * guest page must not turn a renderer-level `window.open()` into a native
 * BrowserWindow, regardless of the requested URL or disposition.
 */
export function installWindowOpenDenyGuard(webContents: Pick<WebContents, 'setWindowOpenHandler'>): void {
  webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
}
