import {
  handoffEmptySessionDraftRevision,
  type PreparedSessionDraftHandoff,
  prepareSessionDraftHandoff
} from './composer'
import {
  type PreparedQueuedPromptsMigration,
  prepareQueuedPromptsMigrationExact
} from './composer-queue'
import {
  finalizeComposerQueueDrainHandoff,
  prepareComposerQueueDrainHandoff,
  type PreparedComposerQueueDrainHandoff
} from './composer-queue-drain'
import {
  _resetComposerStorageScopeAliasesForTests,
  activateComposerStorageScopeAlias,
  decodeComposerStorageScopeKey,
  publishComposerStorageScopeAlias,
  resolveComposerStorageScopeKey
} from './composer-storage-scope'

export { resolveComposerStorageScopeKey } from './composer-storage-scope'

const STORAGE_MIGRATION_PREFIX = 'hermes.desktop.composerStorageMigration.v1:'
const SIMULATED_RENDERER_CRASH = Symbol('simulated-renderer-crash')
let crashAfterQueueForTests = false
let crashAfterAliasForTests = false

interface ComposerStorageMigrationIntent {
  from: string
  to: string
  transactionId: string
}

function intentStorageKey(from: string): string {
  return `${STORAGE_MIGRATION_PREFIX}${from}`
}

function validMigrationPair(from: string, to: string): boolean {
  const fromScope = decodeComposerStorageScopeKey(from)
  const toScope = decodeComposerStorageScopeKey(to)

  return Boolean(
    fromScope &&
      toScope &&
      from !== to &&
      fromScope.owner.connectionId === toScope.owner.connectionId &&
      fromScope.owner.profile === toScope.owner.profile
  )
}

function persistMigrationIntent(intent: ComposerStorageMigrationIntent): void {
  window.localStorage.setItem(intentStorageKey(intent.from), JSON.stringify(intent))
}

function removeMigrationIntent(intent: ComposerStorageMigrationIntent): void {
  try {
    window.localStorage.removeItem(intentStorageKey(intent.from))
  } catch {
    // A completed handoff is idempotent; a retained intent is finalized by the
    // next renderer restart rather than weakening the already-published alias.
  }
}

function readMigrationIntents(): ComposerStorageMigrationIntent[] {
  const intents: ComposerStorageMigrationIntent[] = []

  for (let index = 0; index < window.localStorage.length; index += 1) {
    const storageKey = window.localStorage.key(index)

    if (!storageKey?.startsWith(STORAGE_MIGRATION_PREFIX)) {
      continue
    }

    try {
      const parsed: unknown = JSON.parse(window.localStorage.getItem(storageKey) ?? '')

      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        continue
      }

      const candidate = parsed as Partial<ComposerStorageMigrationIntent>

      if (
        typeof candidate.from === 'string' &&
        typeof candidate.to === 'string' &&
        typeof candidate.transactionId === 'string' &&
        candidate.transactionId &&
        storageKey === intentStorageKey(candidate.from) &&
        validMigrationPair(candidate.from, candidate.to)
      ) {
        intents.push(candidate as ComposerStorageMigrationIntent)
      }
    } catch {
      // Ignore malformed records. Canonical owner validation remains fail-closed.
    }
  }

  return intents
}

function performComposerStorageMigration(intent: ComposerStorageMigrationIntent): void {
  const { from, to, transactionId } = intent

  if (resolveComposerStorageScopeKey(from) === to) {
    const queueHandoff = prepareQueuedPromptsMigrationExact(from, to, transactionId)
    queueHandoff.complete()
    finalizeComposerQueueDrainHandoff(transactionId)
    activateComposerStorageScopeAlias(from, to)
    removeMigrationIntent(intent)

    return
  }

  let draftHandoff: PreparedSessionDraftHandoff | undefined
  let queueHandoff: PreparedQueuedPromptsMigration | undefined
  let drainHandoff: PreparedComposerQueueDrainHandoff | undefined
  let aliasPublished = false

  try {
    draftHandoff = prepareSessionDraftHandoff(from, to)
    queueHandoff = prepareQueuedPromptsMigrationExact(from, to, transactionId)

    if (crashAfterQueueForTests) {
      throw SIMULATED_RENDERER_CRASH
    }

    drainHandoff = prepareComposerQueueDrainHandoff(from, to, transactionId)

    if (!publishComposerStorageScopeAlias(from, to)) {
      throw new Error('Composer storage alias publication was rejected')
    }

    aliasPublished = true
    handoffEmptySessionDraftRevision(from, to)
    activateComposerStorageScopeAlias(from, to)

    if (crashAfterAliasForTests) {
      throw new Error('simulated renderer crash after alias commit')
    }

    queueHandoff.complete()
    drainHandoff.complete()
    draftHandoff.complete()
    removeMigrationIntent(intent)
  } catch (error) {
    if (error === SIMULATED_RENDERER_CRASH || aliasPublished) {
      throw error === SIMULATED_RENDERER_CRASH
        ? new Error('simulated renderer crash after queue commit')
        : error
    }

    let rollbackFailure: unknown

    try {
      queueHandoff?.rollback()
    } catch (rollbackError) {
      rollbackFailure = rollbackError
    }

    try {
      draftHandoff?.rollback()
    } catch (rollbackError) {
      rollbackFailure ??= rollbackError
    }

    try {
      drainHandoff?.rollback()
    } catch (rollbackError) {
      rollbackFailure ??= rollbackError
    }

    if (!rollbackFailure) {
      removeMigrationIntent(intent)
      throw error
    }

    throw new AggregateError([error, rollbackFailure], 'Composer storage migration rollback failed')
  }
}

/**
 * Atomically re-home renderer and main-process composer state for one exact
 * owner. A durable intent precedes every prepared write; the alias is the commit
 * marker and is published only after draft plus queue/park state are durable.
 */
export function migrateComposerStorageScope(fromScopeKey: string, toScopeKey: string): boolean {
  const from = resolveComposerStorageScopeKey(fromScopeKey)
  const to = resolveComposerStorageScopeKey(toScopeKey)

  if (!validMigrationPair(from, to)) {
    return false
  }

  const intent = { from, to, transactionId: `${from}\0${to}` }
  persistMigrationIntent(intent)
  performComposerStorageMigration(intent)

  return true
}

function recoverComposerStorageMigrations(): void {
  for (const intent of readMigrationIntents()) {
    performComposerStorageMigration(intent)
  }
}

/** @internal */
export function _recoverComposerStorageMigrationsForTests(): void {
  recoverComposerStorageMigrations()
}

/** @internal */
export function _setComposerStorageMigrationCrashAfterQueueForTests(enabled: boolean): void {
  crashAfterQueueForTests = enabled
}

/** @internal */
export function _setComposerStorageMigrationCrashAfterAliasForTests(enabled: boolean): void {
  crashAfterAliasForTests = enabled
}

/** @internal */
export function _resetComposerStorageMigrationsForTests(): void {
  crashAfterQueueForTests = false
  crashAfterAliasForTests = false
  _resetComposerStorageScopeAliasesForTests()

  if (typeof window === 'undefined') {
    return
  }

  const intentKeys: string[] = []

  for (let index = 0; index < window.localStorage.length; index += 1) {
    const storageKey = window.localStorage.key(index)

    if (storageKey?.startsWith(STORAGE_MIGRATION_PREFIX)) {
      intentKeys.push(storageKey)
    }
  }

  for (const storageKey of intentKeys) {
    window.localStorage.removeItem(storageKey)
  }
}

if (typeof window !== 'undefined') {
  try {
    recoverComposerStorageMigrations()
  } catch {
    // Startup remains usable; the durable intent retries on the next renderer.
  }
}
