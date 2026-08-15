// Main-process-owned backend target identity for a window.
//
// A BackendTarget is the *resolved* identity of the backend a window's
// gateway connection should target: the primary (window) backend, the profile
// the active connection config maps to, or a profile explicitly forced local
// for this window. It is a closed, typed sum — there are no open extension
// points and no way to smuggle in a raw URL, token, or backend descriptor.
//
// Design invariants enforced here:
//
// - The variants are closed. An unknown kind is rejected at construction.
// - Profile names are validated with the shared profile-name validator, which
//   mirrors the backend's own validate_profile_name(). We never hand the
//   backend a name its resolver would reject.
// - URL/query data is never authority for the target. There is no variant
//   that carries a URL; routing to a remote backend is a connection-config
//   concern resolved upstream, not a property of the target identity.
// - Renderer data never carries tokens, URLs, or raw backend descriptors:
//   the only renderer-reachable inputs to makeBackendTarget are the kind and a
//   validated profile name.
//
// canonicalTargetKey() reduces a target to the pool-identity key. Route mode
// remains part of that identity: a configured profile may resolve remotely,
// while the same profile forced local must use a different pool entry. Window
// ids and scopes never enter the key — only the target does.

import { assertValidProfileName } from './profile-name'

/**
 * The primary (window) backend. The default for any window with no explicit
 * binding.
 */
export interface PrimaryBackendTarget {
  kind: 'primary'
}

/**
 * A profile reached via the active connection config — i.e. the profile the
 * configured route resolves to for this window. The backend may be the primary
 * (when the route maps to the primary profile) or a pool backend.
 */
export interface ConfiguredProfileBackendTarget {
  kind: 'configured-profile'
  profile: string
}

/**
 * A profile explicitly forced local for this window, independent of the
 * connection config. Always resolves to a pool backend keyed by the profile.
 */
export interface ForcedLocalProfileBackendTarget {
  kind: 'forced-local-profile'
  profile: string
}

/**
 * Closed sum of the three legal backend target identities. There is no
 * variant carrying a URL, token, or raw backend descriptor — those are
 * connection/transport concerns, not target identity.
 */
export type BackendTarget =
  | PrimaryBackendTarget
  | ConfiguredProfileBackendTarget
  | ForcedLocalProfileBackendTarget

/**
 * Constructor input for makeBackendTarget. The kind discriminates the variant;
 * profile-carrying variants require a validated profile name.
 */
export type BackendTargetInput = BackendTarget

/**
 * Build and validate a BackendTarget. Rejects unknown kinds and invalid
 * profile names at construction so a malformed target can never propagate into
 * the registry or the pool. This is the only sanctioned way to create a
 * target; constructing the object literal directly is fine for tests but
 * production code should route through here.
 */
export function makeBackendTarget(input: BackendTargetInput): BackendTarget {
  if (!input || typeof input !== 'object' || typeof input.kind !== 'string') {
    throw new Error('Unknown target kind: missing or non-object input')
  }

  const kind = input.kind

  switch (kind) {
    case 'primary':
      return { kind: 'primary' }

    case 'configured-profile':
      assertValidProfileName(input.profile)

      return { kind: 'configured-profile', profile: input.profile }

    case 'forced-local-profile':
      assertValidProfileName(input.profile)

      return { kind: 'forced-local-profile', profile: input.profile }

    default:
      throw new Error(`Unknown target kind: ${kind}`)
  }
}

/**
 * The pool-identity key for a target. Equivalent targets share a key so they
 * share a backend pool entry; distinct targets get distinct keys.
 *
 * - primary -> the fixed sentinel 'primary'.
 * - configured-profile -> 'configured-profile:<name>'.
 * - forced-local-profile -> 'forced-local-profile:<name>'.
 *
 * The mode prefix is load-bearing: configured routing may select a remote
 * backend, while forced-local routing must never share that pool entry.
 *
 * Window ids and scopes never enter the key: the key is derived solely from
 * the target, so the same target always yields the same key regardless of
 * which window it is bound to.
 */
export function canonicalTargetKey(target: BackendTarget): string {
  switch (target.kind) {
    case 'primary':
      return 'primary'

    case 'configured-profile':
      return `configured-profile:${target.profile}`

    case 'forced-local-profile':
      return `forced-local-profile:${target.profile}`

    default:
      // Unreachable for a validated target. The discriminated union narrows
      // to `never` here, which is the compile-time proof the switch is
      // exhaustive; the runtime guard exists only to reject hand-constructed
      // objects that bypassed makeBackendTarget().
      throw new Error('Unknown target kind')
  }
}