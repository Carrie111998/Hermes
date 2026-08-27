import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  ComposerPersistenceCoordinator,
  type ComposerPersistenceState
} from '../../electron/composer-queue-drain-ipc'

import {
  clearSessionDraft,
  clearSessionDraftIfRevision,
  sessionDraftRevision,
  stashSessionDraft,
  takeSessionDraft
} from './composer'
import {
  $parkedQueueSessions,
  $queuedPromptsBySession,
  enqueueQueuedPrompt,
  getQueuedPrompts,
  isQueueParked,
  parkQueuedPrompts
} from './composer-queue'
import {
  _resetComposerQueueDrainsForTests,
  beginComposerQueueDrain,
  finishComposerQueueDrain
} from './composer-queue-drain'
import {
  _resetComposerStorageMigrationsForTests,
  migrateComposerStorageScope,
  resolveComposerStorageScopeKey
} from './composer-storage-migration'
import * as composerStorageMigrationModule from './composer-storage-migration'
import { encodeComposerStorageScopeKey } from './composer-storage-scope'

const OWNER_A = { connectionId: 'connection-a', profile: 'profile-a' }
const OWNER_B = { connectionId: 'connection-b', profile: 'profile-b' }

const key = (owner: typeof OWNER_A, storedSessionId: string | null, newChatGeneration = 0) =>
  encodeComposerStorageScopeKey(owner, storedSessionId, newChatGeneration)

describe('composer storage migration', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: undefined })
    $queuedPromptsBySession.set({})
    $parkedQueueSessions.set({})
    _resetComposerQueueDrainsForTests()
    _resetComposerStorageMigrationsForTests()
  })

  it('atomically hands same-owner state and in-flight drains to the destination', () => {
    const from = key(OWNER_A, null)
    const to = key(OWNER_A, 'stored-1')

    clearSessionDraft(from)
    clearSessionDraft(to)
    stashSessionDraft(from, 'unsent draft', [])
    const entry = enqueueQueuedPrompt(from, { attachments: [], text: 'queued turn' })

    expect(entry).not.toBeNull()
    parkQueuedPrompts(from)
    const drain = beginComposerQueueDrain(from, entry!.id)

    expect(drain).not.toBeNull()
    expect(migrateComposerStorageScope(from, to)).toBe(true)
    expect(takeSessionDraft(from).text).toBe('unsent draft')
    expect(takeSessionDraft(to).text).toBe('unsent draft')
    expect(getQueuedPrompts(from).map(item => item.text)).toEqual(['queued turn'])
    expect(getQueuedPrompts(to).map(item => item.text)).toEqual(['queued turn'])
    expect(isQueueParked(from)).toBe(true)
    expect(isQueueParked(to)).toBe(true)
    expect(resolveComposerStorageScopeKey(from)).toBe(to)
    expect(beginComposerQueueDrain(from, 'late-entry-on-retired-scope')).toBeNull()
    expect(finishComposerQueueDrain(drain!)).toBe(to)
  })

  it('publishes the shared alias only after the migrated queue snapshot', () => {
    const from = key(OWNER_A, null, 9)
    const to = key(OWNER_A, 'stored-live')

    enqueueQueuedPrompt(from, { attachments: [], text: 'queued before handoff' })

    const setItem = vi.spyOn(Storage.prototype, 'setItem')

    expect(migrateComposerStorageScope(from, to)).toBe(true)

    const relevantKeys = setItem.mock.calls
      .map(([storageKey]) => storageKey)
      .filter(storageKey =>
        ['hermes.desktop.composerStorageScopeAliases.v1', 'hermes.desktop.composerQueue.v1'].includes(storageKey)
      )

    expect(relevantKeys.indexOf('hermes.desktop.composerQueue.v1')).toBeLessThan(
      relevantKeys.indexOf('hermes.desktop.composerStorageScopeAliases.v1')
    )
  })

  it('leaves the source authoritative and exposes a main persistence failure', () => {
    const from = key(OWNER_A, null, 10)
    const to = key(OWNER_A, 'stored-failed')

    clearSessionDraft(from)
    clearSessionDraft(to)
    stashSessionDraft(from, 'draft before failed handoff', [])
    enqueueQueuedPrompt(from, { attachments: [], text: 'queue before failed handoff' })
    parkQueuedPrompts(from)

    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        composerPersistence: {
          mutate: () => {
            throw new Error('injected composer persistence failure')
          }
        }
      }
    })

    expect(() => migrateComposerStorageScope(from, to)).toThrow('injected composer persistence failure')
    expect(resolveComposerStorageScopeKey(from)).toBe(from)
    expect(takeSessionDraft(from).text).toBe('draft before failed handoff')
    expect(takeSessionDraft(to).text).toBe('')
    expect(getQueuedPrompts(from).map(item => item.text)).toEqual(['queue before failed handoff'])
    expect(getQueuedPrompts(to)).toHaveLength(0)
    expect(isQueueParked(from)).toBe(true)
    expect(isQueueParked(to)).toBe(false)
  })

  it('rolls back prepared state when durable alias publication fails', () => {
    const from = key(OWNER_A, null, 11)
    const to = key(OWNER_A, 'stored-alias-failed')

    clearSessionDraft(from)
    clearSessionDraft(to)
    stashSessionDraft(from, 'draft before alias failure', [])
    enqueueQueuedPrompt(from, { attachments: [], text: 'queue before alias failure' })
    parkQueuedPrompts(from)

    const setItem = Storage.prototype.setItem
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, storageKey, value) {
      if (storageKey.startsWith('hermes.desktop.composerStorageScopeAlias.v1:')) {
        throw new Error('injected alias persistence failure')
      }

      return setItem.call(this, storageKey, value)
    })

    expect(() => migrateComposerStorageScope(from, to)).toThrow('injected alias persistence failure')
    expect(resolveComposerStorageScopeKey(from)).toBe(from)
    expect(takeSessionDraft(from).text).toBe('draft before alias failure')
    expect(takeSessionDraft(to).text).toBe('')
    expect(getQueuedPrompts(from).map(item => item.text)).toEqual(['queue before alias failure'])
    expect(getQueuedPrompts(to)).toHaveLength(0)
    expect(isQueueParked(from)).toBe(true)
    expect(isQueueParked(to)).toBe(false)
  })

  it('rolls back only source drain claims when alias publication fails', () => {
    const from = key(OWNER_A, null, 13)
    const to = key(OWNER_A, 'stored-drain-rollback')

    enqueueQueuedPrompt(from, { attachments: [], text: 'queue before drain rollback' })
    const sourceDrain = beginComposerQueueDrain(from, 'source-entry')
    const targetDrain = beginComposerQueueDrain(to, 'target-entry')

    expect(sourceDrain).not.toBeNull()
    expect(targetDrain).not.toBeNull()

    const setItem = Storage.prototype.setItem
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, storageKey, value) {
      if (storageKey.startsWith('hermes.desktop.composerStorageScopeAlias.v1:')) {
        throw new Error('injected alias persistence failure')
      }

      return setItem.call(this, storageKey, value)
    })

    expect(() => migrateComposerStorageScope(from, to)).toThrow('injected alias persistence failure')
    expect(finishComposerQueueDrain(sourceDrain!)).toBe(from)
    expect(finishComposerQueueDrain(targetDrain!)).toBe(to)
  })

  it('resumes an interrupted durable handoff after the main coordinator restarts', () => {
    const testApi = composerStorageMigrationModule as typeof composerStorageMigrationModule & {
      _recoverComposerStorageMigrationsForTests?: () => void
      _setComposerStorageMigrationCrashAfterAliasForTests?: (enabled: boolean) => void
      _setComposerStorageMigrationCrashAfterQueueForTests?: (enabled: boolean) => void
    }

    expect(testApi._setComposerStorageMigrationCrashAfterAliasForTests).toEqual(expect.any(Function))

    const from = key(OWNER_A, null, 12)
    const to = key(OWNER_A, 'stored-after-restart')
    let persisted: ComposerPersistenceState | null = null

    const store = {
      load: () => (persisted ? structuredClone(persisted) : null),
      save: (state: ComposerPersistenceState) => {
        persisted = structuredClone(state)
      }
    }

    let coordinator = new ComposerPersistenceCoordinator(store)

    clearSessionDraft(from)
    clearSessionDraft(to)
    stashSessionDraft(from, 'draft survives interrupted handoff', [])
    enqueueQueuedPrompt(from, { attachments: [], text: 'queue survives interrupted handoff' })
    parkQueuedPrompts(from)

    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        composerPersistence: {
          mutate: (request: unknown) => coordinator.mutate(request)
        }
      }
    })

    testApi._setComposerStorageMigrationCrashAfterQueueForTests!(true)
    expect(() => migrateComposerStorageScope(from, to)).toThrow('simulated renderer crash after queue commit')
    expect(resolveComposerStorageScopeKey(from)).toBe(from)

    coordinator = new ComposerPersistenceCoordinator(store)
    testApi._setComposerStorageMigrationCrashAfterQueueForTests!(false)
    testApi._recoverComposerStorageMigrationsForTests!()

    expect(resolveComposerStorageScopeKey(from)).toBe(to)
    expect(takeSessionDraft(from).text).toBe('draft survives interrupted handoff')
    expect(getQueuedPrompts(from).map(item => item.text)).toEqual(['queue survives interrupted handoff'])
    expect(isQueueParked(from)).toBe(true)
  })

  it('finalizes an alias-committed handoff after restart', () => {
    const testApi = composerStorageMigrationModule as typeof composerStorageMigrationModule & {
      _recoverComposerStorageMigrationsForTests: () => void
      _setComposerStorageMigrationCrashAfterAliasForTests: (enabled: boolean) => void
    }

    const from = key(OWNER_A, null, 14)
    const to = key(OWNER_A, 'stored-alias-committed')
    let persisted: ComposerPersistenceState | null = null

    const store = {
      load: () => (persisted ? structuredClone(persisted) : null),
      save: (state: ComposerPersistenceState) => {
        persisted = structuredClone(state)
      }
    }

    let coordinator = new ComposerPersistenceCoordinator(store)

    stashSessionDraft(from, 'draft before committed crash', [])
    enqueueQueuedPrompt(from, { attachments: [], text: 'queue before committed crash' })

    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        composerPersistence: {
          mutate: (request: unknown) => coordinator.mutate(request)
        }
      }
    })

    testApi._setComposerStorageMigrationCrashAfterAliasForTests(true)
    expect(() => migrateComposerStorageScope(from, to)).toThrow('simulated renderer crash after alias commit')
    expect(resolveComposerStorageScopeKey(from)).toBe(to)

    coordinator = new ComposerPersistenceCoordinator(store)
    testApi._setComposerStorageMigrationCrashAfterAliasForTests(false)
    testApi._recoverComposerStorageMigrationsForTests()

    expect(takeSessionDraft(from).text).toBe('draft before committed crash')
    expect(getQueuedPrompts(from).map(item => item.text)).toEqual(['queue before committed crash'])
  })

  it('fails closed across owners without registering an alias or moving state', () => {
    const from = key(OWNER_A, null)
    const to = key(OWNER_B, 'stored-1')

    clearSessionDraft(from)
    clearSessionDraft(to)
    stashSessionDraft(from, 'profile A draft', [])
    enqueueQueuedPrompt(from, { attachments: [], text: 'profile A queue' })

    expect(migrateComposerStorageScope(from, to)).toBe(false)
    expect(resolveComposerStorageScopeKey(from)).toBe(from)
    expect(takeSessionDraft(from).text).toBe('profile A draft')
    expect(takeSessionDraft(to).text).toBe('')
    expect(getQueuedPrompts(from).map(item => item.text)).toEqual(['profile A queue'])
    expect(getQueuedPrompts(to)).toHaveLength(0)
  })

  it('hands off the cleared submitted revision so a late rejection can restore directly', () => {
    const from = key(OWNER_A, null, 7)
    const to = key(OWNER_A, 'stored-created')

    stashSessionDraft(from, 'submitted prompt', [])
    const submittedRevision = clearSessionDraft(from)

    expect(migrateComposerStorageScope(from, to)).toBe(true)
    expect(sessionDraftRevision(from)).toBe(submittedRevision)
    expect(sessionDraftRevision(to)).toBe(submittedRevision)
  })

  it('invalidates a late source completion when the destination already has a colliding revision', () => {
    const from = key(OWNER_A, null, 8)
    const to = key(OWNER_A, 'stored-touched')

    stashSessionDraft(from, 'submitted prompt', [])
    const submittedRevision = clearSessionDraft(from)
    stashSessionDraft(to, 'older destination draft', [])
    stashSessionDraft(to, 'newer destination draft', [])

    expect(sessionDraftRevision(to)).toBe(submittedRevision)
    expect(migrateComposerStorageScope(from, to)).toBe(true)
    expect(clearSessionDraftIfRevision(from, submittedRevision)).toBe(false)
    expect(takeSessionDraft(to).text).toBe('newer destination draft')
  })

  it('keeps the next New Chat generation independent from the previous handoff', () => {
    const firstNewChat = key(OWNER_A, null, 4)
    const created = key(OWNER_A, 'stored-created')
    const nextNewChat = key(OWNER_A, null, 5)

    expect(migrateComposerStorageScope(firstNewChat, created)).toBe(true)
    expect(resolveComposerStorageScopeKey(firstNewChat)).toBe(created)
    expect(resolveComposerStorageScopeKey(nextNewChat)).toBe(nextNewChat)
    expect(nextNewChat).not.toBe(firstNewChat)

    stashSessionDraft(nextNewChat, 'next fresh draft', [])

    expect(takeSessionDraft(nextNewChat).text).toBe('next fresh draft')
    expect(takeSessionDraft(firstNewChat).text).toBe('')
  })
})
