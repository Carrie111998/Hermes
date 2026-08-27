import { decodeComposerStorageScopeKey } from './composer-storage-scope'

export interface ComposerQueueDrainHandle {
  readonly id: number
}

interface ActiveDrain {
  entryId: string
  scopeKey: string
}

let nextDrainId = 1
const activeByHandle = new Map<number, ActiveDrain>()
const handleByEntry = new Map<string, number>()
const handleByScope = new Map<string, number>()

const canonicalScope = (scopeKey: string): boolean => decodeComposerStorageScopeKey(scopeKey)?.format === 'canonical'

/** Atomically claim one qualified queue scope + entry across every drainer. */
export function beginComposerQueueDrain(scopeKey: string, entryId: string): ComposerQueueDrainHandle | null {
  if (!canonicalScope(scopeKey) || !entryId.trim() || handleByScope.has(scopeKey) || handleByEntry.has(entryId)) {
    return null
  }

  const handle = { id: nextDrainId++ }

  activeByHandle.set(handle.id, { entryId, scopeKey })
  handleByScope.set(scopeKey, handle.id)
  handleByEntry.set(entryId, handle.id)

  return handle
}

/** Shared visible/background exclusion query. beginComposerQueueDrain is the atomic form. */
export function isComposerQueueDrainExcluded(scopeKey: string, entryId: string): boolean {
  return !canonicalScope(scopeKey) || handleByScope.has(scopeKey) || handleByEntry.has(entryId)
}

/**
 * Atomically retarget every in-flight claim on a migrated/rekeyed scope. The
 * migration worker needs no private drain token; each drainer's later finish
 * observes the destination key. Returns the number of moved claims.
 */
export function handoffComposerQueueDrains(fromScopeKey: string, toScopeKey: string): number {
  if (!canonicalScope(fromScopeKey) || !canonicalScope(toScopeKey)) {
    return 0
  }

  const handleId = handleByScope.get(fromScopeKey)

  if (handleId === undefined) {
    return 0
  }

  if (fromScopeKey === toScopeKey) {
    return 1
  }

  const targetHolder = handleByScope.get(toScopeKey)

  if (targetHolder !== undefined && targetHolder !== handleId) {
    return 0
  }

  const active = activeByHandle.get(handleId)

  if (!active) {
    return 0
  }

  // Claim the target before releasing the source: no visible/background
  // contender can enter between the two qualified identities.
  handleByScope.set(toScopeKey, handleId)
  handleByScope.delete(fromScopeKey)
  active.scopeKey = toScopeKey

  return 1
}

/** Release a claim and return the qualified key on which it finally settled. */
export function finishComposerQueueDrain(handle: ComposerQueueDrainHandle): string | null {
  const active = activeByHandle.get(handle.id)

  if (!active) {
    return null
  }

  activeByHandle.delete(handle.id)

  if (handleByScope.get(active.scopeKey) === handle.id) {
    handleByScope.delete(active.scopeKey)
  }

  if (handleByEntry.get(active.entryId) === handle.id) {
    handleByEntry.delete(active.entryId)
  }

  return active.scopeKey
}

/** @internal */
export function _resetComposerQueueDrainsForTests(): void {
  activeByHandle.clear()
  handleByEntry.clear()
  handleByScope.clear()
  nextDrainId = 1
}
