export interface UpdateHandoffState {
  updateInFlight: boolean
  handoffInFlight: boolean
}

/**
 * The update gate must stay closed after the detached updater is spawned.
 * Older staged installers do not write their marker until several seconds
 * into startup, so the process-local handoff latch covers that gap.
 */
export function isUpdateOperationBusy(state: UpdateHandoffState): boolean {
  return state.updateInFlight || state.handoffInFlight
}

export type UpdateHandoffExitMode = 'hard' | 'graceful'

/**
 * Windows must bypass Electron quit hooks after the updater owns the handoff;
 * POSIX keeps the existing graceful quit path.
 */
export function updateHandoffExitMode(isWindows: boolean): UpdateHandoffExitMode {
  return isWindows ? 'hard' : 'graceful'
}
