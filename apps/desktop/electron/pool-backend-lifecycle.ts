interface PoolBackendStopDecision {
  activeWorkCount: number
  idleForMs: number
  idleThresholdMs: number
}

/**
 * A renderer keepalive is useful evidence that a pool backend is live, but it is
 * not authoritative: Chromium may throttle or suspend renderer timers while a
 * long tool call continues in the backend. The main process's merged active-work
 * report is the hard veto against terminating any pooled backend mid-turn.
 */
export function canStopIdlePoolBackend({
  activeWorkCount,
  idleForMs,
  idleThresholdMs
}: PoolBackendStopDecision): boolean {
  return activeWorkCount < 1 && idleForMs > idleThresholdMs
}
