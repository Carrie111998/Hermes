/**
 * Sessions-switch send barrier. Blocks turn creation only while a workspace
 * switch is in flight (phase-1 dial or phase-2 commit) or the captured switch
 * generation is stale. Bot Mode talking across machines is not a switch.
 */
export function isWorkspaceSendBlocked(input: {
  capturedGeneration?: number
  currentGeneration?: number
  gatewaySwitching: boolean
  pendingConnectionId: string | null
}): boolean {
  if (input.pendingConnectionId != null) {
    return true
  }

  if (input.gatewaySwitching) {
    return true
  }

  if (
    input.capturedGeneration !== undefined &&
    input.currentGeneration !== undefined &&
    input.capturedGeneration !== input.currentGeneration
  ) {
    return true
  }

  return false
}
