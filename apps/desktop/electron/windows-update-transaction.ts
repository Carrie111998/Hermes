import type { WindowsUpdateState } from './windows-update-state'

export type WindowsUpdateTransactionResult<T> =
  | { kind: 'handed-off'; value: T; preflight: WindowsUpdateState }
  | { kind: 'aborted'; preflight: WindowsUpdateState }
  | { kind: 'reconnect-failed'; preflight: WindowsUpdateState; error: unknown }

export interface WindowsUpdateTransactionDeps<T> {
  preflight: () => Promise<WindowsUpdateState>
  /** The real detached-updater spawn seam. It runs only after a clean handoff decision. */
  spawnUpdater: () => Promise<T>
  /** Re-establish the Desktop backend after a failed/aborted preflight. */
  reconnect: () => Promise<void>
  /** Opens the in-process update gate before reconnecting the backend. */
  releaseUpdateGateBeforeReconnect?: () => void
  onPhase?: (phase: 'result' | 'reconnect' | 'complete' | 'abort') => void
}

/**
 * Execute the caller-level Windows update decision: an aborted preflight never
 * reaches the detached updater, and reconnect is awaited so failure is returned
 * to the update IPC caller instead of becoming an unobserved log side effect.
 */
export async function runWindowsUpdateTransaction<T>(
  deps: WindowsUpdateTransactionDeps<T>
): Promise<WindowsUpdateTransactionResult<T>> {
  const preflight = await deps.preflight()
  if (preflight.decision === 'handoff') {
    return { kind: 'handed-off', value: await deps.spawnUpdater(), preflight }
  }

  deps.onPhase?.('abort')
  deps.onPhase?.('result')
  deps.onPhase?.('reconnect')
  deps.releaseUpdateGateBeforeReconnect?.()
  try {
    await deps.reconnect()
    deps.onPhase?.('complete')
    return { kind: 'aborted', preflight }
  } catch (error) {
    return { kind: 'reconnect-failed', preflight, error }
  }
}
