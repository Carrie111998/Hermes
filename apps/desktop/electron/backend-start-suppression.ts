// Pure predicate for whether a local Hermes backend (re)spawn must be refused.
//
// Extracted from main.ts so the "an update in flight suppresses backend
// respawn" invariant is unit-testable without booting the whole Electron
// update flow (main.ts imports the Electron runtime and cannot be imported
// from vitest).
//
// Why this exists: the Windows update path tears down the desktop's backend
// and, before handing off to the detached updater, runs a venv-blocker
// preflight (scanVenvBlockers). If ANY process is still running from the
// venv's python, the preflight aborts with "venv-blocked" to avoid a
// half-updated venv (native .pyd files stay mapped and the dependency sync
// dies access-denied). But the renderer's getConnection(), wake/reconnect
// recovery, and pool dials all call back into startHermes()/ensureBackend()
// and respawn the backend within milliseconds of the teardown — so the
// preflight always finds a fresh holder and the update is structurally
// impossible while the app is open. Gating every (re)spawn on this predicate
// keeps the venv quiet for the duration of the update.

export function backendStartSuppressionReason(updateActive: boolean): string | null {
  if (updateActive) {
    return 'Backend start suppressed: a Hermes update is in progress.'
  }

  return null
}
