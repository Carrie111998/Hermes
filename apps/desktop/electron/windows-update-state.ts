import type { ScanOutcome, VenvBlockerScanResult } from './venv-blocker-scan'

export type WindowsUpdatePhase = 'blocked' | 'quiescing' | 'rescan' | 'handoff' | 'result' | 'reconnect' | 'abort'
export type WindowsUpdateDecision = 'handoff' | 'reconnect' | 'abort'

export interface WindowsUpdateState {
  phase: WindowsUpdatePhase
  decision?: WindowsUpdateDecision
  blockers?: VenvBlockerScanResult
  error?: string
}

export interface WindowsUpdateStateDeps {
  scan: () => Promise<ScanOutcome>
  /** Graceful stop for documented, Desktop-owned process roots only. */
  quiesceOwned: (processes: VenvBlockerScanResult['processes']) => Promise<boolean>
  /**
   * Local ownership proof for a graceful-stop contract. Scanner metadata is
   * diagnostic only: it must never grant permission to stop a process.
   */
  canQuiesceOwned: (process: VenvBlockerScanResult['processes'][number]) => boolean
  onTransition?: (state: WindowsUpdateState) => void
}

function transition(deps: WindowsUpdateStateDeps, state: WindowsUpdateState): WindowsUpdateState {
  deps.onTransition?.(state)
  return state
}

/**
 * Fail-closed preflight transition machine. It never kills a process. A
 * blocked scan can enter quiescing only when every holder is explicitly marked
 * as supporting a documented graceful protocol; otherwise it aborts/reconnects.
 */
export async function runWindowsUpdatePreflight(deps: WindowsUpdateStateDeps): Promise<WindowsUpdateState> {
  const first = await deps.scan()
  if (first.kind === 'probe-failure') {
    return transition(deps, { phase: 'abort', decision: 'reconnect', error: first.error })
  }
  if (first.kind === 'clear') {
    return transition(deps, { phase: 'handoff', decision: 'handoff' })
  }

  transition(deps, { phase: 'blocked', blockers: first.result })
  if (!first.result.processes.every(process => process.sameInstall && deps.canQuiesceOwned(process))) {
    return transition(deps, { phase: 'abort', decision: 'reconnect', blockers: first.result, error: 'holders require user action' })
  }

  transition(deps, { phase: 'quiescing', blockers: first.result })
  if (!(await deps.quiesceOwned(first.result.processes))) {
    return transition(deps, { phase: 'abort', decision: 'reconnect', blockers: first.result, error: 'graceful quiescence failed' })
  }

  transition(deps, { phase: 'rescan' })
  const second = await deps.scan()
  if (second.kind === 'clear') return transition(deps, { phase: 'handoff', decision: 'handoff' })
  return transition(deps, {
    phase: 'abort', decision: 'reconnect',
    blockers: second.kind === 'blocked' ? second.result : undefined,
    error: second.kind === 'blocked' ? 'holders remain after graceful quiescence' : second.error
  })
}
