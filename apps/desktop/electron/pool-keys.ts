// Route-mode-aware pool key derivation for BackendTargets.
//
// The backend pool was historically keyed by profile name alone. That is
// ambiguous once a window can ask for the same profile via two different
// routes: configured (which may resolve to a remote backend) vs forced-local
// (which must spawn a local profile process and bypass remote resolution).
// Sharing one pool entry for both would let a configured connection steal the
// forced-local entry, or vice versa.
//
// This module is the pure decision layer between a BackendTarget and the pool
// operator (ensureBackend / spawnPoolBackend / stop / touch / revalidate /
// LRU). It derives:
//
// - poolKeyForTarget(target) — the canonical pool key. Equivalent windows
//   share it; configured and forced-local routes for the same profile get
//   distinct keys. This wraps canonicalTargetKey() so the pool operator has a
//   single import for the decision.
// - poolRouteForTarget(target) — the routing branch ('primary' | 'configured'
//   | 'forced-local'), the real profile name to keep on the entry, and the
//   pool key. entry.profile stays the real profile so the backend's own
//   ?profile= scoping still works; the key carries the route mode.
// - isForcedLocalTarget(target) — true when the pool operator must bypass
//   global/profile remote resolution and spawn a local profile process.
//
// The pool operator keeps entry.profile as the real profile name (never the
// key) so the descriptor handed to the renderer and the backend's own
// ?profile= scoping continue to work. The key is pool-internal identity.

import { type BackendTarget, canonicalTargetKey, makeBackendTarget } from './backend-target'

/** The canonical pool key for a target. Equivalent windows share it. */
export function poolKeyForTarget(target: BackendTarget): string {
  if (target.kind === 'configured-connection' && target.connection === 'local') {
    return canonicalTargetKey(makeBackendTarget({ kind: 'forced-local-profile', profile: 'default' }))
  }

  return canonicalTargetKey(target)
}

/** The route-aware storage key for the existing configured-profile path. */
export function configuredPoolKey(profile: string): string {
  return poolKeyForTarget(makeBackendTarget({ kind: 'configured-profile', profile }))
}

/** Find every configured/forced-local pool entry owned by a real profile. */
export function poolKeysForProfile(
  entries: Iterable<readonly [string, { profile?: string }]>,
  profile: string
): string[] {
  const keys: string[] = []

  for (const [key, entry] of entries) {
    if (entry.profile === profile) {
      keys.push(key)
    }
  }

  return keys
}

/** Which routing branch the pool operator should take for this target. */
export type PoolRoute = 'primary' | 'configured' | 'forced-local' | 'connection'

export interface PoolRouteDecision {
  /** Which backend branch to take. */
  route: PoolRoute
  /**
   * The real profile name to keep on the pool entry (entry.profile) and hand
   * to the backend. Null for primary and for a non-local registry connection
   * (those are scoped with ?profile= at request time, not by pool identity).
   */
  profile: null | string
  /** The canonical pool key. */
  key: string
}

/**
 * Derive the pool routing decision for a target. The route tells the pool
 * operator which branch to take; the key is the pool identity; the profile is
 * the real profile name kept on the entry (never the key).
 *
 * `configured-connection:local` shares the forced-local default pool so a
 * "This computer" window does not spawn a second local gateway.
 */
export function poolRouteForTarget(target: BackendTarget): PoolRouteDecision {
  switch (target.kind) {
    case 'primary':
      return { route: 'primary', profile: null, key: canonicalTargetKey(target) }

    case 'configured-profile':
      return { route: 'configured', profile: target.profile, key: canonicalTargetKey(target) }

    case 'forced-local-profile':
      return { route: 'forced-local', profile: target.profile, key: canonicalTargetKey(target) }

    case 'configured-connection':
      if (target.connection === 'local') {
        const localDefault = makeBackendTarget({ kind: 'forced-local-profile', profile: 'default' })

        return { route: 'forced-local', profile: 'default', key: canonicalTargetKey(localDefault) }
      }

      return { route: 'connection', profile: null, key: canonicalTargetKey(target) }

    default:
      // Unreachable for a validated target; the discriminated union narrows
      // to `never` here. The runtime guard exists only to reject objects that
      // bypassed makeBackendTarget().
      throw new Error(`Unknown target kind in pool route`)
  }
}

/**
 * True when the pool operator must bypass global/profile remote resolution for
 * this target and spawn a local profile process. Only forced-local targets
 * (including the local registry connection) do this; primary and configured
 * targets follow the existing ensureBackend(profile) path, which may resolve
 * remotely.
 */
export function isForcedLocalTarget(target: BackendTarget): boolean {
  return (
    target.kind === 'forced-local-profile' ||
    (target.kind === 'configured-connection' && target.connection === 'local')
  )
}