export const ARTIFACT_PREVIEW_PARTITION = 'hermes-artifact-preview'

const ARTIFACT_DATA_URL_PREFIX = 'data:text/html;charset=utf-8,'

interface AttachEvent {
  preventDefault(): void
}

interface AttachParams {
  partition?: string
  src?: string
}

interface GuestSession {
  getPartition?(): string
  setPermissionCheckHandler?(handler: () => boolean): void
  setPermissionRequestHandler?(handler: (...args: unknown[]) => void): void
}

interface GuestWebContents {
  getURL?(): string
  on(event: 'will-navigate', handler: (event: AttachEvent, url: string) => void): void
  session?: GuestSession
  setWindowOpenHandler?(handler: () => { action: 'deny' }): void
}

interface HostWebContents {
  on(
    event: 'will-attach-webview',
    handler: (event: AttachEvent, webPreferences: Record<string, unknown>, params: AttachParams) => void
  ): void
  on(event: 'did-attach-webview', handler: (event: unknown, guest: GuestWebContents) => void): void
}

export function hardenArtifactPreviewWebPreferences(webPreferences: Record<string, unknown>): void {
  delete webPreferences.preload
  delete webPreferences.preloadURL
  Object.assign(webPreferences, {
    allowRunningInsecureContent: false,
    contextIsolation: true,
    nodeIntegration: false,
    sandbox: true,
    webSecurity: true
  })
}

export function installArtifactPreviewGuestIsolation(host: HostWebContents): void {
  host.on('will-attach-webview', (event, webPreferences, params) => {
    if (params.partition !== ARTIFACT_PREVIEW_PARTITION) {
      return
    }

    if (!params.src?.startsWith(ARTIFACT_DATA_URL_PREFIX)) {
      event.preventDefault()

      return
    }

    hardenArtifactPreviewWebPreferences(webPreferences)
  })

  host.on('did-attach-webview', (_event, guest) => {
    if (guest.session?.getPartition?.() !== ARTIFACT_PREVIEW_PARTITION) {
      return
    }

    guest.setWindowOpenHandler?.(() => ({ action: 'deny' }))
    guest.on('will-navigate', (event, url) => {
      if (url !== guest.getURL?.()) {
        event.preventDefault()
      }
    })
    guest.session.setPermissionCheckHandler?.(() => false)
    guest.session.setPermissionRequestHandler?.((_webContents, _permission, callback) => {
      if (typeof callback === 'function') {
        callback(false)
      }
    })
  })
}
