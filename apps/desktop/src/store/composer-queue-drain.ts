import { decodeComposerStorageScopeKey, resolveComposerStorageScopeKey } from './composer-storage-scope'

export interface ComposerQueueDrainHandle {
  readonly id: number
}

interface ActiveDrain {
  entryId: string
  remoteToken?: number
  scopeKey: string
}

let nextDrainId = 1
const activeByHandle = new Map<number, ActiveDrain>()
const handleByEntry = new Map<string, number>()
const handlesByScope = new Map<string, Set<number>>()

const scopeIsClaimed = (scopeKey: string): boolean => Boolean(handlesByScope.get(scopeKey)?.size)
const entryClaimKey = (scopeKey: string, entryId: string): string => `${scopeKey}\0${entryId}`

const desktopArbiter = () => (typeof window === 'undefined' ? undefined : window.hermesDesktop?.composerQueueDrain)

const canonicalScope = (scopeKey: string): string | null => {
  const resolved = resolveComposerStorageScopeKey(scopeKey)

  return decodeComposerStorageScopeKey(resolved)?.format === 'canonical' ? resolved : null
}

/** Atomically claim one qualified queue scope + entry across every drainer. */
export function beginComposerQueueDrain(scopeKey: string, entryId: string): ComposerQueueDrainHandle | null {
  const canonicalKey = canonicalScope(scopeKey)
  const entry = entryId.trim()

  if (!canonicalKey || !entry) {
    return null
  }

  const arbiter = desktopArbiter()
  const remoteToken = arbiter?.begin({ entryId: entry, scopeKey: canonicalKey })
  const qualifiedEntry = entryClaimKey(canonicalKey, entry)

  if (
    (arbiter && remoteToken === null) ||
    (!arbiter && (scopeIsClaimed(canonicalKey) || handleByEntry.has(qualifiedEntry)))
  ) {
    return null
  }

  const handle = { id: nextDrainId++ }

  activeByHandle.set(handle.id, {
    entryId: entry,
    ...(typeof remoteToken === 'number' ? { remoteToken } : {}),
    scopeKey: canonicalKey
  })
  handlesByScope.set(canonicalKey, new Set([handle.id]))
  handleByEntry.set(qualifiedEntry, handle.id)

  return handle
}

/** Shared visible/background exclusion query. beginComposerQueueDrain is the atomic form. */
export function isComposerQueueDrainExcluded(scopeKey: string, entryId: string): boolean {
  const canonicalKey = canonicalScope(scopeKey)

  if (!canonicalKey || !entryId.trim()) {
    return true
  }

  const arbiter = desktopArbiter()

  return arbiter
    ? arbiter.excluded({ entryId: entryId.trim(), scopeKey: canonicalKey })
    : scopeIsClaimed(canonicalKey) || handleByEntry.has(entryClaimKey(canonicalKey, entryId.trim()))
}

/**
 * Atomically retarget every in-flight claim on a migrated/rekeyed scope. The
 * migration worker needs no private drain token; each drainer's later finish
 * observes the destination key. Returns the number of moved claims.
 */
export function handoffComposerQueueDrains(fromScopeKey: string, toScopeKey: string): number {
  const from = canonicalScope(fromScopeKey)
  const to = canonicalScope(toScopeKey)

  if (!from || !to) {
    return 0
  }

  const remoteMoved = desktopArbiter()?.handoff({ fromScopeKey: from, toScopeKey: to })

  const sourceHandles = handlesByScope.get(from)

  if (!sourceHandles?.size) {
    return remoteMoved ?? 0
  }

  if (from === to) {
    return remoteMoved ?? sourceHandles.size
  }

  const targetHandles = handlesByScope.get(to) ?? new Set<number>()
  let moved = 0

  // Claim the target before releasing the source: no visible/background
  // contender can enter between the two qualified identities. An existing
  // destination claim is merged, not overwritten; both must settle before the
  // migrated scope becomes drainable again.
  handlesByScope.set(to, targetHandles)

  for (const handleId of sourceHandles) {
    const active = activeByHandle.get(handleId)

    if (!active) {
      continue
    }

    const previousEntryClaimKey = entryClaimKey(active.scopeKey, active.entryId)

    if (handleByEntry.get(previousEntryClaimKey) === handleId) {
      handleByEntry.delete(previousEntryClaimKey)
    }

    targetHandles.add(handleId)
    active.scopeKey = to
    const nextEntryClaimKey = entryClaimKey(to, active.entryId)

    if (!handleByEntry.has(nextEntryClaimKey)) {
      handleByEntry.set(nextEntryClaimKey, handleId)
    }

    moved += 1
  }

  handlesByScope.delete(from)

  return remoteMoved ?? moved
}

/** Release a claim and return the qualified key on which it finally settled. */
export function finishComposerQueueDrain(handle: ComposerQueueDrainHandle): string | null {
  const active = activeByHandle.get(handle.id)

  if (!active) {
    return null
  }

  const remoteSettledScope = active.remoteToken === undefined ? undefined : desktopArbiter()?.finish(active.remoteToken)

  activeByHandle.delete(handle.id)

  const scopeHandles = handlesByScope.get(active.scopeKey)

  if (scopeHandles) {
    scopeHandles.delete(handle.id)

    if (!scopeHandles.size) {
      handlesByScope.delete(active.scopeKey)
    }
  }

  const qualifiedEntry = entryClaimKey(active.scopeKey, active.entryId)

  if (handleByEntry.get(qualifiedEntry) === handle.id) {
    handleByEntry.delete(qualifiedEntry)
  }

  return remoteSettledScope ?? active.scopeKey
}

/** @internal */
export function _resetComposerQueueDrainsForTests(): void {
  activeByHandle.clear()
  handleByEntry.clear()
  handlesByScope.clear()
  nextDrainId = 1
}
