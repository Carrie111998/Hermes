import type { PreviewUblockInstaller, UblockInstallIntent } from './preview-ublock-installer'

const UBLOCK_DASHBOARD_PATH = 'dashboard.html'

export interface PreviewUblockExtension {
  id: string
  manifest: {
    name?: string
    version?: string
  }
  path: string
  url: string
}

export interface PreviewUblockSession {
  extensions: {
    getAllExtensions(): PreviewUblockExtension[]
    getExtension(extensionId: string): PreviewUblockExtension | null
    loadExtension(path: string, options?: { allowFileAccess?: boolean }): Promise<PreviewUblockExtension>
    removeExtension(extensionId: string): void
  }
}

export interface PreviewUblockState {
  enabled: boolean
  available: boolean
  dashboardUrl: string | null
  extensionId: string | null
  rulesetsReady: boolean
  version: string | null
}

export interface PreviewUblockController {
  dispose(): Promise<void>
  getState(): PreviewUblockState
  initialize(): Promise<PreviewUblockState>
  setEnabled(enabled: boolean): Promise<PreviewUblockState>
}

interface PreviewUblockControllerOptions {
  bootstrap?: (extension: PreviewUblockExtension) => Promise<boolean>
  enabled?: boolean
  installer: Pick<PreviewUblockInstaller, 'resolve'>
  onStaleCache?: (error: unknown) => void
  session: PreviewUblockSession
}

function extensionState(
  extension: PreviewUblockExtension | null,
  enabled: boolean,
  rulesetsReady: boolean
): PreviewUblockState {
  if (!extension) {
    return {
      enabled,
      available: false,
      dashboardUrl: null,
      extensionId: null,
      rulesetsReady: false,
      version: null
    }
  }

  return {
    enabled,
    available: true,
    dashboardUrl: `${extension.url}/${UBLOCK_DASHBOARD_PATH}`,
    extensionId: extension.id,
    rulesetsReady,
    version: extension.manifest.version ?? null
  }
}

export function createPreviewUblockController({
  bootstrap,
  enabled: initiallyEnabled = false,
  installer,
  onStaleCache,
  session: previewSession
}: PreviewUblockControllerOptions): PreviewUblockController {
  let desiredEnabled = initiallyEnabled
  let extension: PreviewUblockExtension | null = null
  let initializePromise: Promise<PreviewUblockState> | null = null
  let lifecycle = Promise.resolve()
  let rulesetsReady = false
  let state = extensionState(null, desiredEnabled, rulesetsReady)

  const enqueue = <T>(operation: () => Promise<T>): Promise<T> => {
    const next = lifecycle.then(operation, operation)
    lifecycle = next.then(
      () => undefined,
      () => undefined
    )

    return next
  }

  const removeLoadedExtension = (): void => {
    if (extension && previewSession.extensions.getExtension(extension.id)) {
      previewSession.extensions.removeExtension(extension.id)
    }

    extension = null
    rulesetsReady = false
  }

  const loadInstall = async (resolved: { path: string; version: string } | null): Promise<PreviewUblockState> => {
    if (!resolved) {throw new Error('uBlock Origin Lite is not installed')}
    const loaded = previewSession.extensions.getAllExtensions().find(item => item.path === resolved.path)

    const candidate =
      loaded ?? (await previewSession.extensions.loadExtension(resolved.path, { allowFileAccess: false }))

    try {
      const ready = bootstrap ? await bootstrap(candidate) : true

      if (!ready) {throw new Error('uBlock Origin Lite rulesets could not be enabled')}
      extension = candidate
      rulesetsReady = true

      return extensionState(extension, true, rulesetsReady)
    } catch (error) {
      if (previewSession.extensions.getExtension(candidate.id)) {previewSession.extensions.removeExtension(candidate.id)}
      throw error
    }
  }

  const loadResolved = async (intent: UblockInstallIntent): Promise<PreviewUblockState> =>
    loadInstall(await installer.resolve(intent))

  const enable = async (): Promise<PreviewUblockState> => {
    removeLoadedExtension()

    try {
      return await loadResolved('latest')
    } catch (latestError) {
      try {
        const fallback = await installer.resolve('cached')
        const result = await loadInstall(fallback)
        onStaleCache?.(latestError)

        return result
      } catch {
        removeLoadedExtension()
        state = extensionState(null, false, false)
        throw latestError
      }
    }
  }

  const initializeEnabled = async (): Promise<PreviewUblockState> => {
    try {
      return await loadResolved('cached')
    } catch {
      removeLoadedExtension()
      state = extensionState(null, false, false)

      return state
    }
  }

  const disable = async (): Promise<PreviewUblockState> => {
    removeLoadedExtension()
    state = extensionState(null, false, false)

    return state
  }

  const controller: PreviewUblockController = {
    async dispose() {
      desiredEnabled = false
      initializePromise = null
      await enqueue(async () => {
        removeLoadedExtension()
        state = extensionState(null, false, false)
      })
    },

    getState() {
      return state
    },

    async initialize() {
      if (!initializePromise) {
        initializePromise = enqueue(() => (desiredEnabled ? initializeEnabled() : disable()))
      }

      return initializePromise
    },

    setEnabled(enabled) {
      desiredEnabled = enabled
      initializePromise = null

      return enqueue(() => (enabled ? enable() : disable()))
    }
  }

  return controller
}

export { UBLOCK_DASHBOARD_PATH }
