// Pure decision: which BackendTarget does an IPC call from a given window
// resolve to, given the window's bound target and the renderer-supplied
// profile argument?
//
// Frozen contract:
//   1. A bound profile target is authoritative when the renderer omits profile
//      OR asks for that target's profile.
//   2. An explicit request for a DIFFERENT profile is marked as a conflict and
//      never changes the bound target.
//   3. An unbound primary window preserves the legacy ensureBackend(profile)
//      behavior for an explicit profile request.
//   4. No renderer-supplied scope: the argument is a profile name (or empty),
//      never a target/scope/url. The decision never trusts a renderer
//      argument as a target identity — it is always treated as a profile
//      name, and a configured-profile target is synthesized for a non-empty
//      different-profile request. The caller (ensureBackend) validates the
//      profile name via the pool routing layer.
//
// This is the pure seam between the IPC handler (which has event.sender and
// the renderer argument) and the pool operator (ensureBackend /
// freshGatewayWsUrl / touchPoolBackend). The IPC handler looks up the window's
// bound target from the registry, calls resolveSenderTarget(), and hands the
// result to the pool operator.

import { type BackendTarget, makeBackendTarget } from './backend-target'
import { profileNameFromRequestPath } from './profile-request-path'

export interface SenderTargetResolution {
  /** The effective target the IPC call routes to. */
  target: BackendTarget
  /**
   * True when the window's bound target overrode the renderer argument (the
   * renderer omitted profile or asked for the target's own profile). False for
   * legacy explicit-profile routing from an unbound primary sender.
   */
  overridden: boolean
  /** True when renderer input attempts to leave the sender's bound target. */
  conflict: boolean
}

/**
 * Decide which BackendTarget an IPC call from a window resolves to.
 *
 * @param boundTarget - the window's bound target from the WindowTargetRegistry.
 *   Never null; the registry returns primary for an unbound window.
 * @param profileArg - the renderer-supplied profile argument (or null/empty).
 *   Never a target/scope/url — always a profile name or nothing.
 */
export function resolveSenderTarget(
  boundTarget: BackendTarget,
  profileArg: null | string | undefined
): SenderTargetResolution {
  const arg = typeof profileArg === 'string' ? profileArg.trim() : ''

  // 1. Renderer omitted profile → use the bound target. A primary-bound
  //    window is the default (no binding), so it is NOT an override; a
  //    profile-bound window IS overriding the (absent) renderer request.
  if (!arg) {
    return { target: boundTarget, overridden: boundTarget.kind !== 'primary', conflict: false }
  }

  // 2. Renderer asked for the bound target's own profile → use the bound
  //    target. Only profile-carrying targets can match a profile arg.
  if (boundTarget.kind !== 'primary' && boundTarget.profile === arg) {
    return { target: boundTarget, overridden: true, conflict: false }
  }

  // A profile-bound sender cannot leave its main-process-owned target.
  if (boundTarget.kind !== 'primary') {
    return { target: boundTarget, overridden: true, conflict: true }
  }

  // An unbound primary window preserves legacy ensureBackend(profile)
  // behavior. The caller validates the profile via the pool routing layer.
  return {
    target: makeBackendTarget({ kind: 'configured-profile', profile: arg }),
    overridden: false,
    conflict: false
  }
}

/**
 * Resolve the target for a newly-created session/HUD window. The selected
 * transcript owner may intentionally differ from the opener target, but main
 * converts that validated profile into a BackendTarget before the child boots.
 */
export function resolveSessionOwnerTarget(
  openerTarget: BackendTarget,
  ownerProfile: null | string | undefined,
  primaryProfile: string
): BackendTarget {
  const owner = typeof ownerProfile === 'string' ? ownerProfile.trim() : ''

  if (!owner) {
    return openerTarget
  }

  if (openerTarget.kind !== 'primary' && openerTarget.profile === owner) {
    return openerTarget
  }

  if (owner === primaryProfile) {
    return makeBackendTarget({ kind: 'primary' })
  }

  return makeBackendTarget({ kind: 'configured-profile', profile: owner })
}

interface SenderRequest {
  body?: unknown
  method?: unknown
  path?: unknown
  profile?: unknown
}

/** Resolve a sender target from the profile channels used by REST IPC. */
export function resolveSenderRequestTarget(
  boundTarget: BackendTarget,
  request: SenderRequest | null | undefined
): SenderTargetResolution {
  const explicit = typeof request?.profile === 'string' ? request.profile.trim() : ''
  const body = request?.body && typeof request.body === 'object' ? request.body : null
  const bodyProfile = body && 'profile' in body && typeof body.profile === 'string' ? body.profile.trim() : ''
  const pathProfile = profileNameFromRequestPath(request?.path) || ''

  let queryProfiles: string[] = []
  let aggregateProfiles: string[] = []
  let aggregateEndpoint = false
  let aggregateRequested = false

  if (typeof request?.path === 'string') {
    try {
      const url = new URL(request.path, 'http://hermes.local')
      const params = url.searchParams
      queryProfiles = params.getAll('profile').map(profile => profile.trim())
      aggregateProfiles = params.getAll('recents_profile').map(profile => profile.trim())

      aggregateEndpoint = url.pathname === '/api/profiles/sessions' || url.pathname === '/api/profiles/sessions/sidebar'

      if (aggregateEndpoint) {
        aggregateRequested =
          explicit === 'all' || bodyProfile === 'all' || queryProfiles.includes('all') || aggregateProfiles.includes('all')
        queryProfiles = queryProfiles.filter(profile => profile !== 'all')
      }
    } catch {
      queryProfiles = []
      aggregateProfiles = []
    }
  }

  const aggregateAuthorities = aggregateProfiles.filter(profile => profile !== 'all')

  const suppliedProfiles = [
    aggregateEndpoint && explicit === 'all' ? '' : explicit,
    ...queryProfiles,
    aggregateEndpoint && bodyProfile === 'all' ? '' : bodyProfile,
    pathProfile,
    ...aggregateAuthorities
  ].filter(Boolean)

  const resolution = resolveSenderTarget(boundTarget, suppliedProfiles[0])

  if (new Set(suppliedProfiles).size > 1 || (aggregateRequested && suppliedProfiles.length > 0)) {
    return { ...resolution, conflict: true }
  }

  return resolution
}

/** Derive list routing only from the already-authorized main-owned target. */
export function sessionProfileForTarget(target: BackendTarget): string {
  return target.kind === 'primary' ? 'all' : target.profile
}

export type ProfileSessionsRoute =
  | { kind: 'local-fast-path' }
  | { kind: 'target'; profile: string }
  | { kind: 'merge' }

/** Keep the primary aggregate fast path unavailable to profile-bound senders. */
export function decideProfileSessionsRoute(
  target: BackendTarget,
  boundTarget: BackendTarget,
  remoteProfiles: string[],
  hasRemoteOverride: (profile: string) => boolean
): ProfileSessionsRoute {
  if (remoteProfiles.length === 0 && boundTarget.kind === 'primary') {
    return { kind: 'local-fast-path' }
  }

  const requested = sessionProfileForTarget(target)

  if (requested === 'all') {
    return { kind: 'merge' }
  }

  if (boundTarget.kind !== 'primary' || hasRemoteOverride(requested)) {
    return { kind: 'target', profile: requested }
  }

  return { kind: 'local-fast-path' }
}

/** Return one main-authoritative batched sidebar path for a bound target. */
export function scopedSidebarPathForTarget(target: BackendTarget, requestPath: string): null | string {
  if (target.kind === 'primary') {
    return null
  }

  const parsed = new URL(requestPath, 'http://hermes.local')

  if (parsed.pathname !== '/api/profiles/sessions/sidebar') {
    return null
  }

  parsed.searchParams.delete('profile')
  parsed.searchParams.delete('recents_profile')
  parsed.searchParams.delete('current_only')
  parsed.searchParams.set('current_only', 'true')

  return `${parsed.pathname}?${parsed.searchParams.toString()}`
}

interface SidebarSession {
  [key: string]: unknown
  is_default_profile?: boolean
  profile?: string
}

interface SidebarSlice {
  [key: string]: unknown
  sessions: SidebarSession[]
  total?: unknown
}

export interface SidebarResponse {
  [key: string]: unknown
  cron: SidebarSlice
  messaging: SidebarSlice
  recents: SidebarSlice & { profile_totals?: Record<string, number> }
}

/** Retag a backend-local sidebar response with the Desktop target alias. */
export function scopeSidebarResponseForTarget(target: BackendTarget, data: unknown): SidebarResponse {
  const response = data as SidebarResponse

  if (target.kind === 'primary') {
    return response
  }

  const retag = (slice: SidebarSlice): SidebarSlice => ({
    ...slice,
    sessions: slice.sessions.map(session => ({
      ...session,
      is_default_profile: false,
      profile: target.profile
    }))
  })

  const recents = retag(response.recents)

  return {
    ...response,
    cron: retag(response.cron),
    messaging: retag(response.messaging),
    recents: {
      ...recents,
      profile_totals: {
        [target.profile]: Number(recents.total) || recents.sessions.length
      }
    }
  }
}