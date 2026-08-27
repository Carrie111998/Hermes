import { claimSessionDraft } from './composer'
import { migrateQueuedPrompts } from './composer-queue'
import { handoffComposerQueueDrains } from './composer-queue-drain'
import {
  _resetComposerStorageScopeAliasesForTests,
  decodeComposerStorageScopeKey,
  registerComposerStorageScopeAlias,
  resolveComposerStorageScopeKey
} from './composer-storage-scope'

export { resolveComposerStorageScopeKey } from './composer-storage-scope'

/**
 * Atomically re-home renderer-only composer state for one exact owner.
 * Canonical decoding is fail-closed, so neither legacy nor malformed keys can
 * cross this migration boundary. The alias keeps late async rejection/settle
 * paths attached to the destination after the visible scope has changed.
 */
export function migrateComposerStorageScope(fromScopeKey: string, toScopeKey: string): boolean {
  const from = resolveComposerStorageScopeKey(fromScopeKey)
  const to = resolveComposerStorageScopeKey(toScopeKey)
  const fromScope = decodeComposerStorageScopeKey(from)
  const toScope = decodeComposerStorageScopeKey(to)

  if (
    !fromScope ||
    !toScope ||
    from === to ||
    fromScope.owner.connectionId !== toScope.owner.connectionId ||
    fromScope.owner.profile !== toScope.owner.profile
  ) {
    return false
  }

  handoffComposerQueueDrains(from, to)
  claimSessionDraft(from, to)
  migrateQueuedPrompts(from, to)
  registerComposerStorageScopeAlias(from, to)

  if (fromScopeKey !== from) {
    registerComposerStorageScopeAlias(fromScopeKey, to)
  }

  return true
}

/** @internal */
export function _resetComposerStorageMigrationsForTests(): void {
  _resetComposerStorageScopeAliasesForTests()
}
