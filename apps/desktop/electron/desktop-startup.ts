export interface DesktopFirstWindowStartupOptions {
  createWindow: () => void
  defer?: (task: () => void) => unknown
  initializeAfterWindow: () => void
  initializeRendererDependencies: () => void
  onBackendError: (error: unknown) => void
  startBackend: () => Promise<unknown>
}

/**
 * Start the backend before renderer-only setup, then create the first window
 * before noncritical shell integrations run. The backend is single-flight, so
 * the renderer's later connection request joins this attempt.
 */
export function startDesktopFirstWindow({
  createWindow,
  defer = task => setImmediate(task),
  initializeAfterWindow,
  initializeRendererDependencies,
  onBackendError,
  startBackend
}: DesktopFirstWindowStartupOptions): void {
  void startBackend().catch(onBackendError)
  initializeRendererDependencies()
  createWindow()
  void defer(initializeAfterWindow)
}
