interface BackendStartupChild {
  exitCode?: number | null
  signalCode?: string | null
  once(event: 'error', listener: (error: unknown) => void): unknown
  once(event: 'exit', listener: (code: number | null, signal: string | null) => void): unknown
  off(event: 'error', listener: (error: unknown) => void): unknown
  off(event: 'exit', listener: (code: number | null, signal: string | null) => void): unknown
}

interface BackendStartupGuardOptions {
  describeExit?: (code: number | null, signal: string | null) => Error
  onFailure?: (error: Error) => void
}

function asError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error))
}

/**
 * Observe a freshly spawned backend while asynchronous ownership persistence
 * runs. The normal lifecycle listeners cannot be installed until the caller
 * has an ownership record, but leaving `error`/`exit` unobserved in that gap
 * can crash Electron or lose the only terminal event.
 *
 * The guard records the first failure, including a child that had already
 * exited before listener installation. Callers install their durable
 * lifecycle listeners, detach this temporary guard, then inspect `failure()`.
 */
export function createBackendStartupGuard(
  child: BackendStartupChild,
  options: BackendStartupGuardOptions = {}
) {
  let startupFailure: Error | null = null

  const fail = (error: unknown) => {
    if (startupFailure) {
      return
    }

    startupFailure = asError(error)
    options.onFailure?.(startupFailure)
  }

  const onError = (error: unknown) => fail(error)
  const onExit = (code: number | null, signal: string | null) => {
    fail(
      options.describeExit?.(code, signal) ??
        new Error(`Hermes backend exited during startup (${signal || code})`)
    )
  }

  child.once('error', onError)
  child.once('exit', onExit)

  // Attach first, then inspect state. This ordering closes both directions of
  // the race: an earlier exit is visible through exitCode/signalCode, while a
  // concurrent exit is delivered to the listener.
  if (child.exitCode != null || child.signalCode != null) {
    onExit(child.exitCode ?? null, child.signalCode ?? null)
  }

  return {
    detach() {
      child.off('error', onError)
      child.off('exit', onExit)
    },

    failure() {
      return startupFailure
    }
  }
}
