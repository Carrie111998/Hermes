// Main-process-owned per-window backend target registry.
//
// Each Electron BrowserWindow is represented by its webContents.id. The
// registry maps that id to the BackendTarget the window's gateway connection
// should target. It is owned by the main process and never sent to the
// renderer; the renderer only ever receives derived, sanitized hints (e.g.
// "your active profile is worker"), never the target model, tokens, URLs, or
// raw backend descriptors.
//
// Semantics:
//
// - default-primary: lookup() on an id with no explicit binding returns the
//   primary target supplied by the registry's resolvePrimaryTarget dep.
// - bind: set an explicit target for an id. The target is re-validated on
//   bind so a hand-constructed object that bypassed makeBackendTarget() is
//   rejected at the registry boundary.
// - inherit-from-opener: a child window (new window, popup) inherits its
//   opener's target if the opener has an explicit binding; otherwise it falls
//   back to primary. This does NOT create a binding for the opener.
// - lookup: return the explicit binding, or the primary target if none.
// - cleanup: remove the explicit binding (on webContents destroy / window
//   close). lookup() afterwards reverts to primary. Idempotent and safe on an
//   unbound id.
//
// The registry has no knowledge of pool keys, backend descriptors, tokens, or
// URLs. It only holds validated BackendTargets keyed by webContents.id. The
// canonical pool key for a binding is derived via canonicalTargetKey() by the
// caller when it needs to talk to the pool; the id itself is never a pool key.

import { type BackendTarget, canonicalTargetKey, makeBackendTarget } from './backend-target'

export interface WindowTargetRegistryDeps {
  /**
   * Returns the primary target used as the default for any id without an
   * explicit binding. Injected so the registry stays pure / testable without
   * importing the main-process primary resolver.
   */
  resolvePrimaryTarget: () => BackendTarget
}

export interface WindowTargetRegistry {
  /** Bind (or rebind) an explicit validated target to a webContents.id. */
  bind: (webContentsId: number, target: BackendTarget) => void

  /**
   * Make a child window inherit its opener's target. Falls back to primary
   * when the opener has no explicit binding or is unknown. Does not create a
   * binding for the opener.
   */
  inheritFromOpener: (childWebContentsId: number, openerWebContentsId: number) => void

  /** Return the target for an id: the explicit binding, or the primary. */
  lookup: (webContentsId: number) => BackendTarget

  /** Whether the id's explicit binding was revoked and must reject IPC. */
  isRevoked: (webContentsId: number) => boolean

  /** Remove the explicit binding for an id; lookup() afterwards is primary. */
  cleanup: (webContentsId: number) => void

  /** Remove every explicit binding that targets the named profile. */
  revokeProfile: (profile: string) => number[]

  /** Whether two webContents ids currently resolve to the same target. */
  hasSameTarget: (leftWebContentsId: number, rightWebContentsId: number) => boolean
}

/**
 * Create a per-window backend target registry. Pure and synchronous; the
 * only dep is resolvePrimaryTarget(), injected so the registry can be
 * unit-tested without Electron.
 */
export function createWindowTargetRegistry(deps: WindowTargetRegistryDeps): WindowTargetRegistry {
  const bindings = new Map<number, BackendTarget>()
  const revokedBindings = new Set<number>()

  const primary = (): BackendTarget => {
    try {
      const resolved = makeBackendTarget(deps.resolvePrimaryTarget())

      return resolved.kind === 'primary' ? resolved : makeBackendTarget({ kind: 'primary' })
    } catch {
      return makeBackendTarget({ kind: 'primary' })
    }
  }

  return {
    bind(webContentsId, target) {
      // Re-validate at the boundary: a caller that built the target by hand
      // (or got it from an untrusted source) must not be able to poison the
      // registry with an unvalidated profile name.
      const validated = makeBackendTarget(target)
      bindings.set(webContentsId, validated)
      revokedBindings.delete(webContentsId)
    },

    inheritFromOpener(childWebContentsId, openerWebContentsId) {
      const openerTarget = bindings.get(openerWebContentsId)

      if (openerTarget) {
        // Copy the opener's target by value via re-validation, so the child
        // gets an independent validated target, not a shared reference.
        bindings.set(childWebContentsId, makeBackendTarget(openerTarget))

        if (revokedBindings.has(openerWebContentsId)) {
          revokedBindings.add(childWebContentsId)
        } else {
          revokedBindings.delete(childWebContentsId)
        }
      } else {
        bindings.delete(childWebContentsId)
        revokedBindings.delete(childWebContentsId)
      }
      // If the opener has no explicit binding, the child defaults to primary —
      // we intentionally do NOT set a binding here, so the child inherits the
      // live primary resolution rather than a snapshot.
    },

    lookup(webContentsId) {
      return makeBackendTarget(bindings.get(webContentsId) ?? primary())
    },

    isRevoked(webContentsId) {
      return revokedBindings.has(webContentsId)
    },

    cleanup(webContentsId) {
      bindings.delete(webContentsId)
      revokedBindings.delete(webContentsId)
    },

    revokeProfile(profile) {
      const revoked: number[] = []

      for (const [webContentsId, target] of bindings) {
        if (target.kind !== 'primary' && target.profile === profile) {
          revokedBindings.add(webContentsId)
          revoked.push(webContentsId)
        }
      }

      return revoked
    },

    hasSameTarget(leftWebContentsId, rightWebContentsId) {
      return canonicalTargetKey(this.lookup(leftWebContentsId)) === canonicalTargetKey(this.lookup(rightWebContentsId))
    }
  }
}

interface RevocableWindow {
  destroy: () => void
  isDestroyed: () => boolean
  webContents: { id: number }
}

/** Force-close windows whose main-owned backend bindings were revoked. */
export function destroyRevokedWindows(webContentsIds: number[], windows: RevocableWindow[]): void {
  const revoked = new Set(webContentsIds)

  for (const window of windows) {
    if (revoked.has(window.webContents.id) && !window.isDestroyed()) {
      window.destroy()
    }
  }
}