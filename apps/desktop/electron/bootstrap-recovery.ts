/**
 * Recovery policy for the boot-failure overlay's Retry / Repair actions.
 *
 * Transient local gateway failures (GIL stalls, ready-frame races) surface as
 * "Could not connect to Hermes gateway". Historically both Retry and Repair
 * tore down a still-alive backend, and Repair additionally forced a full
 * installer re-run against a usable runtime — which recreated the stall and
 * looped forever (#74874 / #72391 / #71226).
 *
 * Runtime usability (not the failure message) decides whether Repair may
 * reinstall. A ready, live backend decides whether Retry may skip teardown.
 */

export type BootstrapRecoveryKind = 'repair' | 'reset'

export interface BootstrapRecoveryInput {
  kind: BootstrapRecoveryKind
  /** Active install at ACTIVE_HERMES_ROOT is launchable without bootstrap. */
  runtimeUsable: boolean
  /** Primary backend child is still alive. */
  backendAlive: boolean
  /** startHermes already published a ready connection for that child. */
  connectionReady: boolean
}

export interface BootstrapRecoveryPlan {
  /** Bypass the usable active runtime and re-run the installer. */
  forceInstaller: boolean
  /** Kill the primary backend before the renderer reloads. */
  teardownBackend: boolean
  log: string
}

export function decideBootstrapRecovery(input: BootstrapRecoveryInput): BootstrapRecoveryPlan {
  if (input.kind === 'repair') {
    if (input.runtimeUsable) {
      // Soft repair: clear latches and respawn through the existing runtime.
      // Reinstalling cannot fix a GIL stall and is what creates the loop.
      return {
        forceInstaller: false,
        teardownBackend: true,
        log: '[bootstrap] repair requested; active runtime is usable — soft-restarting without reinstall'
      }
    }

    return {
      forceInstaller: true,
      teardownBackend: true,
      log: '[bootstrap] repair requested by renderer; forcing reinstall + clearing latched failure'
    }
  }

  // Retry: when main already declared the backend ready and the child is still
  // alive, the failure was a transient renderer↔WS race. Keep the process so
  // the reload re-dials the same port instead of cold-starting into another
  // stall.
  if (input.backendAlive && input.connectionReady) {
    return {
      forceInstaller: false,
      teardownBackend: false,
      log: '[bootstrap] reset requested; reusing live ready backend (no teardown)'
    }
  }

  return {
    forceInstaller: false,
    teardownBackend: true,
    log: '[bootstrap] reset requested by renderer; clearing latched failure'
  }
}
