export const RENDERER_CLOSE_REQUEST_CHANNEL = 'hermes:window:close-barrier:request'
export const RENDERER_CLOSE_RESULT_CHANNEL = 'hermes:window:close-barrier:result'
export const RENDERER_CLOSE_TIMEOUT_MS = 4_000

export type RendererCloseFailureReason = 'destroyed' | 'unavailable' | 'unresponsive' | 'vetoed'

export interface RendererCloseWindow {
  close: () => void
  isDestroyed: () => boolean
  on: (event: string, listener: (event: { preventDefault: () => void }) => void) => unknown
  removeListener?: (event: string, listener: (event: { preventDefault: () => void }) => void) => unknown
  webContents: {
    id: number
    send: (channel: string, payload: unknown) => void
  }
}

interface PendingRendererClose {
  promise: Promise<boolean>
  requestId: string
  resolve: (allowed: boolean) => void
  timeout: unknown
  window: RendererCloseWindow
}

export interface RendererCloseCoordinatorOptions {
  clearScheduled?: (timeout: unknown) => void
  nextRequestId?: () => string
  onFailure?: (failure: { reason: RendererCloseFailureReason; window: RendererCloseWindow }) => void
  scheduleTimeout?: (callback: () => void, timeoutMs: number) => unknown
  timeoutMs?: number
}

function closeResult(value: unknown): value is { allowed: boolean; requestId: string } {
  if (!value || typeof value !== 'object') {
    return false
  }

  const result = value as { allowed?: unknown; requestId?: unknown }

  return typeof result.allowed === 'boolean' && typeof result.requestId === 'string'
}

/**
 * Correlates renderer flush acknowledgements with the BrowserWindow that made
 * the request. A missing, crashed, or unresponsive renderer fails closed.
 */
export function createRendererCloseCoordinator(options: RendererCloseCoordinatorOptions = {}) {
  const pendingByWebContents = new Map<number, PendingRendererClose>()
  const clearScheduled = options.clearScheduled ?? (timeout => clearTimeout(timeout as NodeJS.Timeout))
  const scheduleTimeout = options.scheduleTimeout ?? ((callback, timeoutMs) => setTimeout(callback, timeoutMs))
  const timeoutMs = options.timeoutMs ?? RENDERER_CLOSE_TIMEOUT_MS
  let next = 0
  const nextRequestId = options.nextRequestId ?? (() => String(++next))

  const settle = (pending: PendingRendererClose, allowed: boolean, reason?: RendererCloseFailureReason): void => {
    if (pendingByWebContents.get(pending.window.webContents.id) !== pending) {
      return
    }

    pendingByWebContents.delete(pending.window.webContents.id)
    clearScheduled(pending.timeout)

    if (reason) {
      options.onFailure?.({ reason, window: pending.window })
    }

    pending.resolve(allowed)
  }

  return {
    request(window: RendererCloseWindow): Promise<boolean> {
      if (window.isDestroyed()) {
        options.onFailure?.({ reason: 'destroyed', window })

        return Promise.resolve(false)
      }

      const existing = pendingByWebContents.get(window.webContents.id)

      if (existing) {
        return existing.promise
      }

      const requestId = nextRequestId()
      let resolve!: (allowed: boolean) => void
      const promise = new Promise<boolean>(done => {
        resolve = done
      })
      const pending: PendingRendererClose = {
        promise,
        requestId,
        resolve,
        timeout: undefined,
        window
      }
      pending.timeout = scheduleTimeout(() => settle(pending, false, 'unresponsive'), timeoutMs)
      pendingByWebContents.set(window.webContents.id, pending)

      try {
        window.webContents.send(RENDERER_CLOSE_REQUEST_CHANNEL, { requestId })
      } catch {
        settle(pending, false, 'unavailable')
      }

      return pending.promise
    },
    resolve(webContentsId: number, value: unknown): boolean {
      if (!closeResult(value)) {
        return false
      }

      const pending = pendingByWebContents.get(webContentsId)

      if (!pending || pending.requestId !== value.requestId) {
        return false
      }

      settle(pending, value.allowed, value.allowed ? undefined : 'vetoed')

      return true
    }
  }
}

export interface AppCloseBarrierOptions {
  onFailure?: () => void
  requestFlush: () => Promise<boolean>
  retryClose: () => void
}

/**
 * Coordinates app-wide before-quit retries. A downstream guard that delays the
 * retried quit must call rearm(), because renderers remain interactive while
 * that guard is pending and can create new close-sensitive work.
 */
export function createAppCloseBarrier(options: AppCloseBarrierOptions) {
  let phase: 'flushing' | 'idle' | 'permitted' = 'idle'

  return {
    hold(event: { preventDefault: () => void }): boolean {
      if (phase === 'permitted') {
        return false
      }

      event.preventDefault()

      if (phase === 'flushing') {
        return true
      }

      phase = 'flushing'
      void options.requestFlush().then(
        allowed => {
          if (!allowed) {
            phase = 'idle'
            options.onFailure?.()

            return
          }

          phase = 'permitted'
          options.retryClose()
        },
        () => {
          phase = 'idle'
          options.onFailure?.()
        }
      )

      return true
    },
    isPermitted(): boolean {
      return phase === 'permitted'
    },
    rearm(): void {
      if (phase === 'permitted') {
        phase = 'idle'
      }
    }
  }
}

export interface WindowCloseBarrierOptions {
  isTeardownPermitted?: () => boolean
  onFailure?: () => void
  requestFlush: () => Promise<boolean>
}

/**
 * Turns Electron's synchronous `close` event into one vetoed asynchronous
 * flush, followed by one permitted retry. Repeated close events share the
 * in-flight request instead of recursively starting more flushes.
 */
export function installWindowCloseBarrier(window: RendererCloseWindow, options: WindowCloseBarrierOptions): () => void {
  let phase: 'flushing' | 'idle' | 'permitted' = 'idle'
  let disposed = false

  const onClose = (event: { preventDefault: () => void }) => {
    if (disposed || phase === 'permitted' || options.isTeardownPermitted?.()) {
      return
    }

    event.preventDefault()

    if (phase === 'flushing') {
      return
    }

    phase = 'flushing'
    void options.requestFlush().then(
      allowed => {
        if (!allowed || window.isDestroyed()) {
          phase = 'idle'
          options.onFailure?.()

          return
        }

        phase = 'permitted'

        try {
          window.close()
        } catch {
          phase = 'idle'
          options.onFailure?.()
        }
      },
      () => {
        phase = 'idle'
        options.onFailure?.()
      }
    )
  }

  window.on('close', onClose)

  return () => {
    if (disposed) {
      return
    }

    disposed = true
    window.removeListener?.('close', onClose)
  }
}
