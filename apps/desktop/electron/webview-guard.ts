import type { Event, WebContents, WebPreferences } from 'electron'

/** The one partition used by the user-facing local preview webview. */
export const PREVIEW_WEBVIEW_PARTITION = 'persist:hermes-preview'

const LOCAL_PREVIEW_HOSTS = new Set(['127.0.0.1', '0.0.0.0', '::1', '[::1]', 'localhost'])

const WINDOWS_UNC_HOST_RE =
  /^(?=.{1,255}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*$/i

export interface PreviewWebviewUrlOptions {
  /**
   * Shape-check a Windows UNC URL. Callers must still validate the canonical
   * path against the normalized preview target before enabling this option.
   */
  allowWindowsUnc?: boolean
}

function isWindowsUncFileUrl(url: URL): boolean {
  return (
    Boolean(url.hostname) &&
    WINDOWS_UNC_HOST_RE.test(url.hostname) &&
    !url.username &&
    !url.password &&
    !url.port &&
    url.pathname.length > 1
  )
}

/**
 * Preview webviews intentionally render local files and local development
 * servers. No other scheme or remote host is an intended guest source.
 */
export function isAllowedPreviewWebviewUrl(rawUrl: string, options: PreviewWebviewUrlOptions = {}): boolean {
  let url: URL

  try {
    url = new URL(String(rawUrl || '').trim())
  } catch {
    return false
  }

  if (url.protocol === 'file:') {
    return !url.hostname || Boolean(options.allowWindowsUnc && isWindowsUncFileUrl(url))
  }

  return (url.protocol === 'http:' || url.protocol === 'https:') && LOCAL_PREVIEW_HOSTS.has(url.hostname.toLowerCase())
}

/**
 * Keep an attached preview guest on the same local/file allowlist as its
 * initial source. The event payloads for these Electron navigation events all
 * expose the destination as `event.url`, including subframe navigations.
 */
export function installPreviewWebviewNavigationGuard(
  webContents: Pick<WebContents, 'on'>,
  isAllowedUrl: (rawUrl: string) => boolean = isAllowedPreviewWebviewUrl
): void {
  // `will-frame-navigate` and `will-redirect` also describe child frames. The
  // preview boundary applies to the top-level document; sandboxing and the
  // browser's normal frame policy continue to govern about:blank/srcdoc and
  // provider/app subframes without breaking legitimate preview content.
  const denyOffAllowlist = (event: Pick<Event, 'preventDefault'> & { isMainFrame?: boolean }, url: string) => {
    if (event.isMainFrame === false) {
      return
    }

    if (!isAllowedUrl(url)) {
      event.preventDefault()
    }
  }

  webContents.on('will-navigate', event => denyOffAllowlist(event, event.url))
  webContents.on('will-frame-navigate', event => denyOffAllowlist(event, event.url))
  webContents.on('will-redirect', event => denyOffAllowlist(event, event.url))
}

/**
 * Enforce the preview guest policy before Electron attaches a webview.
 * Renderer attributes are attacker-controlled at this boundary, so the
 * intended source and partition are checked and all preload/Node-capable
 * preferences are replaced with the safe preview values.
 */
export function enforcePreviewWebviewPolicy(
  event: Pick<Event, 'preventDefault'>,
  webPreferences: WebPreferences,
  params: Record<string, string>,
  isAllowedUrl: (rawUrl: string) => boolean = isAllowedPreviewWebviewUrl
): void {
  const requestedPartition = params.partition?.trim()

  const allowed = isAllowedUrl(params.src) && (!requestedPartition || requestedPartition === PREVIEW_WEBVIEW_PARTITION)

  if (!allowed) {
    event.preventDefault()

    return
  }

  // Electron accepts these renderer-supplied attributes before this event;
  // remove them from both representations so they cannot be re-applied by
  // the attachment machinery after this callback returns.
  delete params.preload
  delete params.webpreferences
  delete params.allowpopups
  delete params.nodeintegration
  delete params.nodeintegrationinsubframes
  delete params.plugins
  delete params.blinkfeatures
  delete params.disableblinkfeatures
  params.partition = PREVIEW_WEBVIEW_PARTITION

  delete webPreferences.preload
  delete webPreferences.enableBlinkFeatures
  delete webPreferences.disableBlinkFeatures
  webPreferences.allowRunningInsecureContent = false
  webPreferences.contextIsolation = true
  webPreferences.experimentalFeatures = false
  webPreferences.javascript = true
  webPreferences.nodeIntegration = false
  webPreferences.nodeIntegrationInSubFrames = false
  webPreferences.nodeIntegrationInWorker = false
  webPreferences.partition = PREVIEW_WEBVIEW_PARTITION
  webPreferences.plugins = false
  webPreferences.sandbox = true
  webPreferences.webSecurity = true
  webPreferences.webviewTag = false
}
