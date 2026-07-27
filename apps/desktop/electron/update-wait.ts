import { readLiveUpdateMarker } from './update-marker'

export interface UpdateMarkerState {
  pid: number
  ageMs: number
}

export type UpdateWaitReceipt =
  | { stage: 'update-wait'; status: 'skipped' }
  | { stage: 'update-wait'; status: 'completed' | 'timed-out'; updaterPid: number }

/** Refuse to start a backend while the bounded update gate still has a live owner. */
export function requireUpdateGateCompletion(receipt: UpdateWaitReceipt): void {
  if (receipt.status === 'timed-out') {
    throw new Error(
      `Hermes update did not finish before the startup timeout; updater process ${receipt.updaterPid} is still active.`
    )
  }
}

export interface UpdateWaitDeps {
  readMarker?: (hermesHome: string) => UpdateMarkerState | null
  now?: () => number
  sleep?: (ms: number) => Promise<void>
  onProgress?: () => Promise<void>
  log?: (message: string) => void
  timeoutMs?: number
  pollMs?: number
}

const DEFAULT_TIMEOUT_MS = 20 * 60 * 1000
const DEFAULT_POLL_MS = 1000

/**
 * Gate local backend startup while an updater owns the installation.
 * The explicit terminal receipt prevents callers from conflating a verified
 * marker disappearance with the bounded timeout fallback.
 */
export async function waitForUpdateGate(
  hermesHome: string,
  deps: UpdateWaitDeps = {}
): Promise<UpdateWaitReceipt> {
  const readMarker = deps.readMarker ?? readLiveUpdateMarker
  const now = deps.now ?? Date.now
  const sleep = deps.sleep ?? (ms => new Promise(resolve => setTimeout(resolve, ms)))
  const onProgress = deps.onProgress ?? (async () => {})
  const log = deps.log ?? (() => {})
  const timeoutMs = deps.timeoutMs ?? DEFAULT_TIMEOUT_MS
  const pollMs = deps.pollMs ?? DEFAULT_POLL_MS
  let marker = readMarker(hermesHome)

  if (!marker) {
    return { stage: 'update-wait', status: 'skipped' }
  }

  const updaterPid = marker.pid
  log(`update in progress (pid=${updaterPid}); deferring backend start until it finishes`)
  const deadline = now() + timeoutMs

  while (marker && now() < deadline) {
    await onProgress()
    await sleep(pollMs)
    marker = readMarker(hermesHome)
  }

  if (marker) {
    log('update still in progress after wait timeout; refusing backend startup')

    return { stage: 'update-wait', status: 'timed-out', updaterPid }
  }

  log('update finished; proceeding with backend start')

  return { stage: 'update-wait', status: 'completed', updaterPid }
}
