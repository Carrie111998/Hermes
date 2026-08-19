/**
 * Fail-closed lifecycle control for desktop-owned backend children.
 *
 * A numeric PID is observation, never destructive authority. This module only
 * signals a retained ChildProcess-style owner whose lifecycle fields still say
 * it is live. Persisted or reconstructed `{ pid }` records are intentionally
 * rejected. If no owner survives, callers must leave residue and abort the
 * operation rather than rediscovering and killing by PID (#89614).
 *
 * Tree-wide force-stop belongs in a platform authority created at spawn time:
 * a retained Windows Job Object handle or a retained POSIX process-group
 * owner. Until that authority lands, direct-child shutdown plus abort-on-lock
 * is the safe P1 policy.
 */

export interface BackendProcessRoot {
  pid?: number | null
  exitCode?: null | number
  signalCode?: null | string
}

export interface KillableChild extends BackendProcessRoot {
  killed?: boolean
  /** Accepts NodeJS.Signals (e.g. 'SIGTERM', 'SIGKILL') to match ChildProcess.kill. */
  kill: (signal?: NodeJS.Signals | number | null) => unknown
}

export interface StopBackendTreesForUpdateDeps {
  /** Stops pooled backends through their retained ChildProcess owners. */
  stopAllPoolBackends: () => Promise<void> | void
}

/**
 * True only for a retained owner with a positive PID and explicit live
 * lifecycle state. Missing lifecycle fields mean there is no authority, not
 * that a legacy PID should be trusted.
 */
export function isLiveProcessRoot(root: BackendProcessRoot | null | undefined): boolean {
  return Boolean(
    root &&
      Number.isInteger(root.pid) &&
      (root.pid as number) > 0 &&
      root.exitCode === null &&
      root.signalCode === null
  )
}

function signalRetainedChild(child: KillableChild | null | undefined, signal: NodeJS.Signals): boolean {
  if (!child || !isLiveProcessRoot(child) || typeof child.kill !== 'function') {
    return false
  }
  try {
    return child.kill(signal) !== false
  } catch {
    return false
  }
}

/** Graceful stop through the retained owner only. */
export function stopBackendChild(child: KillableChild | null | undefined): boolean {
  if (!child || child.killed) {
    return false
  }
  return signalRetainedChild(child, 'SIGTERM')
}

/** Forced direct-child stop through the same retained owner only. */
export function forceStopBackendChild(child: KillableChild | null | undefined): boolean {
  return signalRetainedChild(child, 'SIGKILL')
}

/**
 * Begin update teardown without ever converting ownership into a PID.
 * Descendants that survive direct-child shutdown keep the shim locked; the
 * caller must then abort the update. That residue is deliberate containment
 * until a retained Job Object/process-group authority owns the full tree.
 */
export async function stopBackendTreesForUpdate(
  primary: KillableChild | null | undefined,
  deps: StopBackendTreesForUpdateDeps
): Promise<void> {
  stopBackendChild(primary)
  await deps.stopAllPoolBackends()
}
