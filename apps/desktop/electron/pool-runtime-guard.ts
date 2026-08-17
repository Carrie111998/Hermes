/**
 * pool-runtime-guard.ts
 *
 * Pool backends are SECONDARY agent sources: background profiles and the
 * multi-connection registry's rows. They are started by enumeration and by
 * UI affordances, not by the user asking to install Hermes.
 *
 * They must therefore never reach the first-run bootstrap. This is the same
 * rule the `hermes:window:openInTerminal` handler already states in main.ts —
 * "Resolution only -- never ensureRuntime(), which would kick off a first-run
 * install from a menu click; an unresolved runtime is reported instead."
 *
 * Without this guard, a remote-primary install with no local runtime has a
 * full platform install (clone + venv + dependency install) started by the
 * agent roster merely ENUMERATING the registry's 'local' row.
 */

export interface ResolvedBackendLike {
  kind?: unknown
}

/** Message surfaced on the unreachable source row instead of installing. */
export const POOL_LOCAL_RUNTIME_MISSING =
  'No local Hermes runtime is installed on this machine. Install Hermes locally to use "This device" as an agent source.'

/**
 * True when resolveHermesBackend() found nothing to spawn and would hand off
 * to the bootstrap runner.
 */
export function poolBackendNeedsBootstrap(backend: null | ResolvedBackendLike | undefined): boolean {
  return Boolean(backend && typeof backend === 'object' && (backend as ResolvedBackendLike).kind === 'bootstrap-needed')
}

/**
 * Refuse rather than install. Callers spawning a POOL backend must call this
 * before ensureRuntime(); the thrown error is caught per-source by the roster
 * enumeration and rendered as an unreachable row.
 */
export function assertPoolRuntimeInstalled(backend: null | ResolvedBackendLike | undefined): void {
  if (poolBackendNeedsBootstrap(backend)) {
    throw new Error(POOL_LOCAL_RUNTIME_MISSING)
  }
}
