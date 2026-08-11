// Profile-delete routing logic for the `hermes:api` IPC handler.
//
// When the renderer issues DELETE /api/profiles/<name>, the handler must
// tear down that profile's backend (primary window backend or pool backend)
// and then route the *next* request away from the just-deleted profile's
// pool backend -- spawning a fresh one would call ensure_hermes_home() and
// recreate the profile directory the delete just removed, leaving a zombie
// process behind (issue #52279).
//
// These helpers are pure so they can be unit-tested without Electron.

import { type BackendTarget, makeBackendTarget } from './backend-target'
import { PROFILE_NAME_RE } from './profile-name'
import { isProfileCollectionPath, profileNameFromRequestPath, requestPathname } from './profile-request-path'

/**
 * Parse a `hermes:api` request into the profile name a DELETE targets, or
 * null when the request is not a profile-delete at all (wrong method, wrong
 * path, empty/invalid name).
 */
export function profileNameFromDeleteRequest(request) {
  if (!request || String(request.method || 'GET').toUpperCase() !== 'DELETE') {
    return null
  }

  const pathname = requestPathname(request.path)

  if (!pathname || !/^\/api\/profiles\/[^/]+$/.test(pathname)) {
    return null
  }

  const name = profileNameFromRequestPath(request.path)

  if (!name) {
    return null
  }

  if (name.toLowerCase() === 'default') {
    return 'default'
  }

  return name
}

export function profileNameFromCreateRequest(request) {
  if (
    !request ||
    String(request.method || 'GET').toUpperCase() !== 'POST' ||
    !isProfileCollectionPath(request.path)
  ) {
    return null
  }

  const name = typeof request.body?.name === 'string' ? request.body.name.trim().toLowerCase() : ''

  return PROFILE_NAME_RE.test(name) && name !== 'default' ? name : null
}

export function assertProfileNotRevoked(
  profile: string | null,
  isRevoked: (profile: string) => boolean
): void {
  if (profile && isRevoked(profile)) {
    throw new Error(`Profile "${profile}" is being deleted and cannot be connected.`)
  }
}

export async function settleStartedOperations(operations: Array<() => Promise<void>>): Promise<void> {
  const noFailure = Symbol('no-failure')
  let firstFailure: unknown | typeof noFailure = noFailure
  const started = operations.map(async operation => operation())

  await Promise.all(
    started.map(async operation => {
      try {
        await operation
      } catch (error) {
        if (firstFailure === noFailure) {
          firstFailure = error
        }
      }
    })
  )

  if (firstFailure !== noFailure) {
    throw firstFailure
  }
}

export async function runProfileMutationPreflight<T, M>(
  mutation: M | null,
  operation: (handoff: () => void, track: (mutation: M) => void) => Promise<T>,
  settle: (mutation: M, succeeded: boolean) => void
): Promise<T> {
  let handedOff = false
  let settled = false
  let trackedMutation = mutation

  const settleTracked = (succeeded: boolean): void => {
    if (trackedMutation !== null && !handedOff && !settled) {
      settled = true
      settle(trackedMutation, succeeded)
    }
  }

  try {
    const result = await operation(
      () => {
        handedOff = true
      },
      nextMutation => {
        if (handedOff) {
          throw new Error('Cannot track a profile mutation after request handoff.')
        }

        trackedMutation = nextMutation
      }
    )

    settleTracked(true)

    return result
  } catch (error) {
    settleTracked(false)

    throw error
  }
}

export interface ProfileRename {
  from: string
  to: string
}

export function profileRenameFromRequest(request): ProfileRename | null {
  if (!request || String(request.method || 'GET').toUpperCase() !== 'PATCH') {
    return null
  }

  const pathname = requestPathname(request.path)

  if (!pathname || !/^\/api\/profiles\/[^/]+$/.test(pathname)) {
    return null
  }

  const from = profileNameFromRequestPath(request.path)
  const to = typeof request.body?.new_name === 'string' ? request.body.new_name.trim().toLowerCase() : ''

  if (!from || from === 'default' || !PROFILE_NAME_RE.test(to) || to === 'default') {
    return null
  }

  return { from, to }
}

export type ProfileRenameAction = 'teardown-primary' | 'teardown-pool'

export function decideProfileRenameAction(rename: ProfileRename, primaryProfile: string) {
  return {
    action: rename.from === primaryProfile ? 'teardown-primary' : 'teardown-pool',
    ...rename
  } as const
}

export interface ProfileRenameLifecycleDeps<T> {
  completeRevocation: (mutation: T) => void
  destroyRevokedWindows: (webContentsIds: number[]) => void
  failRevocation: (mutation: T) => void
  migrateConnectionOverride: (from: string, to: string) => void
  revokeProfile: (profile: string) => T
  revokeWindowTargets: (profile: string) => number[]
  teardownPrimary: () => Promise<void>
  teardownProfilePools: (profile: string) => Promise<void>
  writeActiveProfile: (profile: string) => void
}

export async function applyProfileRenameLifecycle<T>(
  rename: ProfileRename,
  primaryProfile: string,
  deps: ProfileRenameLifecycleDeps<T>
): Promise<void> {
  const decision = decideProfileRenameAction(rename, primaryProfile)
  const mutation = deps.revokeProfile(decision.from)

  try {
    const revokedWindowIds = deps.revokeWindowTargets(decision.from)

    deps.destroyRevokedWindows(revokedWindowIds)
    deps.migrateConnectionOverride(decision.from, decision.to)

    if (decision.action === 'teardown-primary') {
      deps.writeActiveProfile(decision.to)
      await settleStartedOperations([deps.teardownPrimary, () => deps.teardownProfilePools(decision.from)])
    } else {
      await deps.teardownProfilePools(decision.from)
    }

    deps.completeRevocation(mutation)
  } catch (error) {
    deps.failRevocation(mutation)
    throw error
  }
}

export function createProfileRevocationGuard() {
  const revoked = new Set<string>()
  const pendingDeletes = new Map<string, number>()
  const pendingCreates = new Map<string, Set<number>>()
  const latestMutations = new Map<string, ProfileMutationToken>()
  const successfulCreates = new Set<number>()
  const successfulDeletes = new Set<number>()
  const deletesOvertakenByOlderCreates = new Set<number>()
  const retirableDeletes = new Set<number>()
  let nextEpoch = 0

  const startMutation = (profile: string, kind: ProfileMutationKind): ProfileMutationToken => {
    const previous = latestMutations.get(profile)

    if (previous) {
      successfulCreates.delete(previous.epoch)
      successfulDeletes.delete(previous.epoch)
      deletesOvertakenByOlderCreates.delete(previous.epoch)
      retirableDeletes.delete(previous.epoch)
    }

    const mutation = { epoch: ++nextEpoch, kind, profile }

    latestMutations.set(profile, mutation)

    return mutation
  }

  const forgetLatestMutation = (profile: string, clearRevocation: boolean): void => {
    const latest = latestMutations.get(profile)

    if (latest) {
      successfulCreates.delete(latest.epoch)
      successfulDeletes.delete(latest.epoch)
      deletesOvertakenByOlderCreates.delete(latest.epoch)
      retirableDeletes.delete(latest.epoch)
    }

    latestMutations.delete(profile)

    if (clearRevocation) {
      revoked.delete(profile)
    }
  }

  const restoreLatestSuccessfulCreate = (profile: string): boolean => {
    const latest = latestMutations.get(profile)

    if (
      latest?.kind === 'create' &&
      successfulCreates.has(latest.epoch) &&
      !pendingDeletes.has(profile)
    ) {
      forgetLatestMutation(profile, true)

      return true
    }

    return false
  }

  const retireLatestSuccessfulDelete = (profile: string): boolean => {
    const latest = latestMutations.get(profile)
    const pending = pendingCreates.get(profile)

    const hasOlderPendingCreate = pending
      ? [...pending].some(createEpoch => createEpoch < (latest?.epoch || 0))
      : false

    if (
      latest?.kind === 'delete' &&
      successfulDeletes.has(latest.epoch) &&
      !deletesOvertakenByOlderCreates.has(latest.epoch) &&
      !hasOlderPendingCreate &&
      !pendingDeletes.has(profile)
    ) {
      const canRetire = retirableDeletes.has(latest.epoch)

      forgetLatestMutation(profile, canRetire)

      return canRetire
    }

    return false
  }

  return {
    isRevoked(profile: string): boolean {
      return revoked.has(profile)
    },
    isCurrent(mutation: ProfileMutationToken): boolean {
      return latestMutations.get(mutation.profile)?.epoch === mutation.epoch
    },
    startCreation(profile: string): ProfileMutationToken {
      const mutation = startMutation(profile, 'create')
      const pending = pendingCreates.get(profile) || new Set<number>()

      pending.add(mutation.epoch)
      pendingCreates.set(profile, pending)

      return mutation
    },
    revoke(profile: string): ProfileMutationToken {
      const mutation = startMutation(profile, 'delete')

      revoked.add(profile)
      pendingDeletes.set(profile, (pendingDeletes.get(profile) || 0) + 1)

      return mutation
    },
    restore(profile: string): void {
      pendingDeletes.delete(profile)
      forgetLatestMutation(profile, true)
    },
    completeMutation({
      mutation,
      retireSucceeded = true,
      succeeded
    }: {
      mutation: ProfileMutationToken
      retireSucceeded?: boolean
      succeeded: boolean
    }): ProfileMutationCompletion {
      const { epoch, kind, profile } = mutation

      if (kind === 'delete') {
        const pending = pendingDeletes.get(profile) || 0

        if (pending > 1) {
          pendingDeletes.set(profile, pending - 1)
        } else {
          pendingDeletes.delete(profile)
        }

        if (latestMutations.get(profile)?.epoch === epoch) {
          if (succeeded) {
            successfulDeletes.add(epoch)

            if (retireSucceeded) {
              retirableDeletes.add(epoch)
            }
          } else {
            // Ambiguous response failure remains fail-closed. A later
            // successful creation is the only event that restores authority.
            revoked.add(profile)
          }
        }

        if (restoreLatestSuccessfulCreate(profile)) {
          return { retiredProfile: null }
        }

        return {
          retiredProfile: retireLatestSuccessfulDelete(profile) ? profile : null
        }
      }

      const pending = pendingCreates.get(profile)

      pending?.delete(epoch)

      if (pending?.size === 0) {
        pendingCreates.delete(profile)
      }

      const latest = latestMutations.get(profile)

      if (
        succeeded &&
        latest?.kind === 'delete' &&
        latest.epoch > epoch &&
        successfulDeletes.has(latest.epoch)
      ) {
        // This older request completed after the newer delete and may have
        // recreated the profile. Keep the delete tombstone fail-closed.
        deletesOvertakenByOlderCreates.add(latest.epoch)
      }

      if (succeeded && latest?.epoch === epoch) {
        successfulCreates.add(epoch)
        restoreLatestSuccessfulCreate(profile)
      } else if (!succeeded && latest?.epoch === epoch && !pendingDeletes.has(profile)) {
        // A failed standalone create needs no tombstone and must not retain its
        // mutation token. Preserve any earlier deletion revocation.
        forgetLatestMutation(profile, false)
      }

      return {
        retiredProfile: retireLatestSuccessfulDelete(profile) ? profile : null
      }
    }
  }
}

export type ProfileMutationKind = 'create' | 'delete'

export interface ProfileMutationToken {
  epoch: number
  kind: ProfileMutationKind
  profile: string
}

export interface ProfileMutationCompletion {
  retiredProfile: string | null
}

export interface DesktopConnectionConfigLike {
  [key: string]: unknown
  profiles?: Record<string, unknown>
}

/** Remove one deleted profile route without changing global or sibling settings. */
export function removeProfileConnectionOverride<T extends DesktopConnectionConfigLike>(
  config: T,
  profile: string
): T {
  const profiles = { ...(config.profiles || {}) }

  delete profiles[profile]

  return { ...config, profiles }
}

/** Move one renamed profile route without changing global or sibling settings. */
export function renameProfileConnectionOverride<T extends DesktopConnectionConfigLike>(
  config: T,
  from: string,
  to: string
): T {
  if (!Object.hasOwn(config.profiles || {}, from)) {
    return { ...config, profiles: { ...(config.profiles || {}) } }
  }

  const profiles = { ...(config.profiles || {}), [to]: config.profiles?.[from] }

  delete profiles[from]

  return { ...config, profiles }
}

export type ProfileDeleteAction = 'noop' | 'teardown-primary' | 'teardown-pool'

export interface ProfileDeleteDecision {
  action: ProfileDeleteAction
  profile: string | null
}

export interface ProfileDeleteDecisionDeps {
  isDefaultProfile: (profile: string) => boolean
  isValidProfileName: (profile: string) => boolean
  primaryProfileKey: () => string
}

/**
 * Pure decision logic for prepareProfileDeleteRequest: given the parsed
 * profile name (or null), decide which side-effecting branch the caller
 * should take and what profile name it should ultimately report as
 * torn-down. No I/O, no async -- the caller performs the actual teardown
 * based on `action`.
 */
export function decideProfileDeleteAction(
  profile: string | null,
  deps: ProfileDeleteDecisionDeps
): ProfileDeleteDecision {
  if (!profile || deps.isDefaultProfile(profile) || !deps.isValidProfileName(profile)) {
    return { action: 'noop', profile: null }
  }

  if (profile === deps.primaryProfileKey()) {
    return { action: 'teardown-primary', profile }
  }

  return { action: 'teardown-pool', profile }
}

export interface ProfileDeleteLifecycleDeps<T> {
  destroyRevokedWindows: (webContentsIds: number[]) => void
  failRevocation: (mutation: T) => void
  revokeProfile: (profile: string) => T
  revokeWindowTargets: (profile: string) => number[]
  teardownPrimary: () => Promise<void>
  teardownProfileBackends: (profile: string) => Promise<void>
  writeActiveProfile: (profile: string) => void
}

export async function applyProfileDeleteLifecycle<T>(
  decision: ProfileDeleteDecision,
  deps: ProfileDeleteLifecycleDeps<T>
): Promise<{ mutation: T | null; profile: string | null }> {
  if (decision.action === 'noop' || !decision.profile) {
    return { mutation: null, profile: null }
  }

  const mutation = deps.revokeProfile(decision.profile)

  try {
    const revokedWindowIds = deps.revokeWindowTargets(decision.profile)

    deps.destroyRevokedWindows(revokedWindowIds)

    if (decision.action === 'teardown-primary') {
      deps.writeActiveProfile('default')
      await settleStartedOperations([deps.teardownPrimary, () => deps.teardownProfileBackends(decision.profile)])
    } else {
      await deps.teardownProfileBackends(decision.profile)
    }

    return { mutation, profile: decision.profile }
  } catch (error) {
    deps.failRevocation(mutation)
    throw error
  }
}

/**
 * Route the next `hermes:api` request away from the primary/window backend
 * whenever a profile was just torn down -- otherwise ensureBackend would
 * spawn a fresh pool backend for the deleted profile, whose
 * ensure_hermes_home() recreates the directory the delete just removed.
 */
export function resolveRouteTarget(tornDownProfile: string | null, authorizedTarget: BackendTarget): BackendTarget {
  return tornDownProfile ? makeBackendTarget({ kind: 'primary' }) : authorizedTarget
}
