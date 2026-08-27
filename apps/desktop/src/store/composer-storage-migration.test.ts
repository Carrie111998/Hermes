import { beforeEach, describe, expect, it } from 'vitest'

import { clearSessionDraft, sessionDraftRevision, stashSessionDraft, takeSessionDraft } from './composer'
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
import { encodeComposerStorageScopeKey } from './composer-storage-scope'

const OWNER_A = { connectionId: 'connection-a', profile: 'profile-a' }
const OWNER_B = { connectionId: 'connection-b', profile: 'profile-b' }

const key = (owner: typeof OWNER_A, storedSessionId: string | null, newChatGeneration = 0) =>
  encodeComposerStorageScopeKey(owner, storedSessionId, newChatGeneration)

describe('composer storage migration', () => {
  beforeEach(() => {
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
