// Preview-pane <webview> guest wiring.
//
// HTML reports (Playwright, coverage, etc.) open attachment screenshots via
// target=_blank. Without a guest handler those navigations either spawn a dead
// window or replace the report with Chromium's bare image viewer — and the
// preview chrome has no Back control, so the only obvious X closes the whole
// pane. Wire every preview webview so:
//   1. window.open / target=_blank navigates in-place (history stays intact)
//   2. Escape goes back one history entry when the guest can go back
// The renderer surfaces an explicit Back titlebar tool from canGoBack().

import type { App, WebContents } from 'electron'
import { webContents as electronWebContents } from 'electron'

type WindowOpenDetails = { url: string }

type InputEventLike = {
  key?: string
  type?: string
}

type GuestWebContents = Pick<WebContents, 'canGoBack' | 'goBack' | 'isDestroyed' | 'loadURL'> & {
  id?: number
  on: (event: string, listener: (...args: any[]) => void) => void
  once?: (event: string, listener: (...args: any[]) => void) => void
  setWindowOpenHandler: (handler: (details: WindowOpenDetails) => { action: 'allow' | 'deny' }) => void
}

const wiredGuestIds = new Set<number>()

/** In-place navigation keeps history so Back/Escape can restore the report. */
export function previewWindowOpenDecision(_url: string): 'navigate-in-place' {
  return 'navigate-in-place'
}

/** Escape means "leave the attachment view", not "close the preview pane". */
export function shouldHandleEscapeAsPreviewBack(
  input: InputEventLike,
  canGoBack: boolean
): boolean {
  if (!canGoBack) {
    return false
  }

  if (input.type && input.type !== 'keyDown') {
    return false
  }

  return String(input.key || '') === 'Escape'
}

export function wirePreviewWebviewContents(contents: GuestWebContents): void {
  const guestId = typeof contents.id === 'number' ? contents.id : null

  if (guestId != null) {
    if (wiredGuestIds.has(guestId)) {
      return
    }

    wiredGuestIds.add(guestId)
    contents.once?.('destroyed', () => {
      wiredGuestIds.delete(guestId)
    })
  }

  contents.setWindowOpenHandler(details => {
    if (previewWindowOpenDecision(details.url) === 'navigate-in-place') {
      // Defer so we don't navigate during the handler (Electron rejects that).
      // setImmediate is more reliable than queueMicrotask for webContents.loadURL
      // inside setWindowOpenHandler on Electron 40 guests.
      setImmediate(() => {
        if (contents.isDestroyed()) {
          return
        }

        void contents.loadURL(details.url).catch((error: unknown) => {
          const message = error instanceof Error ? error.message : String(error)
          console.warn(`[preview-webview] in-place open failed: ${message} (${details.url})`)
        })
      })
    }

    return { action: 'deny' }
  })

  contents.on('before-input-event', (event: { preventDefault: () => void }, input: InputEventLike) => {
    if (!shouldHandleEscapeAsPreviewBack(input, contents.canGoBack())) {
      return
    }

    event.preventDefault()
    contents.goBack()
  })
}

/**
 * Install once at app ready. Every subsequent <webview> guest (preview pane
 * partition or otherwise) gets the same back-friendly window.open + Escape
 * behavior. The renderer also calls wirePreviewWebviewById after mount so we
 * still win if type detection on web-contents-created is delayed/odd.
 */
export function installPreviewWebviewGuards(electronApp: Pick<App, 'on'>): void {
  electronApp.on('web-contents-created', (_event, contents) => {
    const type = (contents as WebContents).getType?.()
    // Only <webview> guests. BrowserWindows already have their own open handlers.
    if (type !== 'webview') {
      return
    }

    wirePreviewWebviewContents(contents as unknown as GuestWebContents)
  })
}

/** Explicit wire from the renderer after a preview <webview> mounts. */
export function wirePreviewWebviewById(
  webContentsId: number,
  webContentsApi: Pick<typeof electronWebContents, 'fromId'> = electronWebContents
): boolean {
  if (!Number.isFinite(webContentsId) || webContentsId <= 0) {
    return false
  }

  if (wiredGuestIds.has(webContentsId)) {
    return true
  }

  const contents = webContentsApi.fromId(webContentsId)

  if (!contents || contents.isDestroyed()) {
    return false
  }

  wirePreviewWebviewContents(contents as unknown as GuestWebContents)

  return wiredGuestIds.has(webContentsId) || true
}
