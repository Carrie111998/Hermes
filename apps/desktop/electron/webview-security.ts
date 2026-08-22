import type { WebContents } from 'electron'

type GuardableContents = Pick<WebContents, 'getType' | 'setWindowOpenHandler'>

/**
 * Deny every popup/new-window request created by an untrusted Browser Pane
 * guest. The renderer deliberately embeds arbitrary web pages in a sandboxed
 * `<webview>`; Electron CVE-2026-70608 proves the missing `allowpopups`
 * attribute is not itself a sufficient boundary on affected releases.
 */
export function guardUntrustedWebviewWindowOpen(contents: GuardableContents): boolean {
  if (contents.getType() !== 'webview') {
    return false
  }

  contents.setWindowOpenHandler(() => ({ action: 'deny' }))

  return true
}
