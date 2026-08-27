import { beforeEach, describe, expect, it } from 'vitest'

import type { ComposerAttachment } from './composer'
import {
  $parkedQueueSessions,
  $queuedPromptsBySession,
  clearQueuedPrompts,
  clearQueuedPromptsForOwnerLineage,
  dequeueQueuedPrompt,
  enqueueQueuedPrompt,
  getQueuedPrompts,
  isQueueParked,
  migrateQueuedPrompts,
  parkQueuedPrompts,
  promoteQueuedPrompt,
  reloadPersistedComposerQueue,
  removeQueuedPrompt,
  shouldAutoDrain,
  unparkQueuedPrompts,
  updateQueuedPrompt,
  updateQueuedPromptText
} from './composer-queue'
import { encodeComposerStorageScopeKey } from './composer-storage-scope'

const SESSION_KEY = 'session-abc'
const QUEUE_STORAGE_KEY = 'hermes.desktop.composerQueue.v1'
const PARK_STORAGE_KEY = 'hermes.desktop.composerQueueParks.v1'

function attachment(id: string, kind: ComposerAttachment['kind'] = 'file'): ComposerAttachment {
  return {
    id,
    kind,
    label: id,
    refText: `@file:${id}`
  }
}

describe('composer queue store', () => {
  beforeEach(() => {
    window.localStorage.removeItem(QUEUE_STORAGE_KEY)
    window.localStorage.removeItem(PARK_STORAGE_KEY)
    $queuedPromptsBySession.set({})
    $parkedQueueSessions.set({})
  })

  it('queues prompts in FIFO order', () => {
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'first' })
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'second' })

    expect(dequeueQueuedPrompt(SESSION_KEY)?.text).toBe('first')
    expect(dequeueQueuedPrompt(SESSION_KEY)?.text).toBe('second')
    expect(dequeueQueuedPrompt(SESSION_KEY)).toBeNull()
  })

  it('dequeues the head from a fresh persisted snapshot', () => {
    const staleHead = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'stale local head' })!
    const externalHead = { ...staleHead, id: 'external-head', text: 'new persisted head' }

    window.localStorage.setItem(QUEUE_STORAGE_KEY, JSON.stringify({ [SESSION_KEY]: [externalHead, staleHead] }))

    expect(dequeueQueuedPrompt(SESSION_KEY)?.text).toBe('new persisted head')
    expect(getQueuedPrompts(SESSION_KEY).map(entry => entry.text)).toEqual(['stale local head'])
  })

  it('reloads a queue removal written by another BrowserWindow', () => {
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'already sent elsewhere' })
    window.localStorage.removeItem(QUEUE_STORAGE_KEY)

    reloadPersistedComposerQueue()

    expect(getQueuedPrompts(SESSION_KEY)).toHaveLength(0)
  })

  it('appends from one fresh persisted snapshot when another window enqueues first', () => {
    const first = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'first window entry' })!
    const external = { ...first, id: 'external-entry', text: 'other window entry' }

    window.localStorage.setItem(QUEUE_STORAGE_KEY, JSON.stringify({ [SESSION_KEY]: [first, external] }))

    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'local follow-up' })

    expect(getQueuedPrompts(SESSION_KEY).map(entry => entry.text)).toEqual([
      'first window entry',
      'other window entry',
      'local follow-up'
    ])
  })

  it('persists Stop parks so another BrowserWindow cannot drain them as unparked', () => {
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'hold this turn' })
    parkQueuedPrompts(SESSION_KEY)

    expect(JSON.parse(window.localStorage.getItem(PARK_STORAGE_KEY) ?? '{}')).toEqual({ [SESSION_KEY]: true })
  })

  it('clears canonical lineage queues only for the deleted exact owner', () => {
    const ownerA = { connectionId: 'source-a', profile: 'worker' }
    const ownerB = { connectionId: 'source-b', profile: 'worker' }
    const keyA = encodeComposerStorageScopeKey(ownerA, 'shared-id')
    const keyB = encodeComposerStorageScopeKey(ownerB, 'shared-id')

    enqueueQueuedPrompt(keyA, { attachments: [], text: 'owner A queued turn' })
    enqueueQueuedPrompt(keyB, { attachments: [], text: 'owner B queued turn' })

    clearQueuedPromptsForOwnerLineage(ownerA, ['shared-id'])

    expect(getQueuedPrompts(keyA)).toHaveLength(0)
    expect(getQueuedPrompts(keyB).map(entry => entry.text)).toEqual(['owner B queued turn'])
  })

  it('clones attachments when queueing', () => {
    const source = [attachment('a-1')]
    const queued = enqueueQueuedPrompt(SESSION_KEY, { attachments: source, text: 'check clones' })

    expect(queued).not.toBeNull()
    expect(getQueuedPrompts(SESSION_KEY)[0]?.attachments[0]).toEqual(source[0])
    expect(getQueuedPrompts(SESSION_KEY)[0]?.attachments[0]).not.toBe(source[0])
  })

  it('updates and removes queued entries by id', () => {
    const first = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'draft one' })
    const second = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'draft two' })

    expect(first).not.toBeNull()
    expect(second).not.toBeNull()

    expect(updateQueuedPromptText(SESSION_KEY, first!.id, 'draft one edited')).toBe(true)
    expect(getQueuedPrompts(SESSION_KEY).map(entry => entry.text)).toEqual(['draft one edited', 'draft two'])

    expect(removeQueuedPrompt(SESSION_KEY, first!.id)).toBe(true)
    expect(getQueuedPrompts(SESSION_KEY).map(entry => entry.text)).toEqual(['draft two'])
  })

  it('removes from one fresh persisted snapshot without resurrecting another window removal', () => {
    const first = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'removed in other window' })!
    const second = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'remove locally' })!
    const external = { ...first, id: 'external-entry', text: 'added in other window' }

    window.localStorage.setItem(QUEUE_STORAGE_KEY, JSON.stringify({ [SESSION_KEY]: [second, external] }))

    expect(removeQueuedPrompt(SESSION_KEY, second.id)).toBe(true)
    expect(getQueuedPrompts(SESSION_KEY).map(entry => entry.text)).toEqual(['added in other window'])
  })

  it('promotes a queued entry to the front', () => {
    const first = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'first' })
    const second = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'second' })
    const third = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'third' })

    expect(first).not.toBeNull()
    expect(second).not.toBeNull()
    expect(third).not.toBeNull()

    expect(promoteQueuedPrompt(SESSION_KEY, third!.id)).toBe(true)
    expect(getQueuedPrompts(SESSION_KEY).map(entry => entry.text)).toEqual(['third', 'first', 'second'])
    expect(promoteQueuedPrompt(SESSION_KEY, third!.id)).toBe(false)
  })

  it('promotes within a fresh persisted snapshot', () => {
    const first = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'first' })!
    const second = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'second' })!
    const external = { ...first, id: 'external-entry', text: 'added elsewhere' }

    window.localStorage.setItem(QUEUE_STORAGE_KEY, JSON.stringify({ [SESSION_KEY]: [external, first, second] }))

    expect(promoteQueuedPrompt(SESSION_KEY, second.id)).toBe(true)
    expect(getQueuedPrompts(SESSION_KEY).map(entry => entry.text)).toEqual(['second', 'added elsewhere', 'first'])
  })

  it('updates queued text and attachment snapshot', () => {
    const first = enqueueQueuedPrompt(SESSION_KEY, { attachments: [attachment('f-1')], text: 'draft one' })
    const editedAttachments = [attachment('f-2'), attachment('f-3', 'image')]

    expect(first).not.toBeNull()
    expect(
      updateQueuedPrompt(SESSION_KEY, first!.id, {
        attachments: editedAttachments,
        text: 'edited text'
      })
    ).toBe(true)

    const queue = getQueuedPrompts(SESSION_KEY)
    expect(queue[0]?.text).toBe('edited text')
    expect(queue[0]?.attachments).toEqual(editedAttachments)
    expect(queue[0]?.attachments[0]).not.toBe(editedAttachments[0])
  })

  it('updates within a fresh persisted snapshot', () => {
    const stale = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'removed elsewhere' })!
    const target = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'edit me' })!
    const external = { ...stale, id: 'external-entry', text: 'added elsewhere' }

    window.localStorage.setItem(QUEUE_STORAGE_KEY, JSON.stringify({ [SESSION_KEY]: [target, external] }))

    expect(updateQueuedPromptText(SESSION_KEY, target.id, 'edited')).toBe(true)
    expect(getQueuedPrompts(SESSION_KEY).map(entry => entry.text)).toEqual(['edited', 'added elsewhere'])
  })

  it('clears queue state for a session', () => {
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [attachment('img-1', 'image')], text: 'queued' })

    clearQueuedPrompts(SESSION_KEY)

    expect(getQueuedPrompts(SESSION_KEY)).toEqual([])
    expect($queuedPromptsBySession.get()[SESSION_KEY]).toBeUndefined()
    expect(window.localStorage.getItem(QUEUE_STORAGE_KEY)).toBeNull()
  })

  it('clears a fresh persisted scope even when the local atom has not received the storage event', () => {
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'external queue' })
    $queuedPromptsBySession.set({})

    clearQueuedPrompts(SESSION_KEY)

    expect(window.localStorage.getItem(QUEUE_STORAGE_KEY)).toBeNull()
    expect(getQueuedPrompts(SESSION_KEY)).toEqual([])
  })

  it('persists queue entries into local storage', () => {
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'persist me' })

    const raw = window.localStorage.getItem(QUEUE_STORAGE_KEY)
    expect(raw).toBeTruthy()

    const parsed = JSON.parse(String(raw)) as Record<string, { text: string }[]>
    expect(parsed[SESSION_KEY]?.[0]?.text).toBe('persist me')
  })
})

describe('migrateQueuedPrompts', () => {
  beforeEach(() => {
    window.localStorage.removeItem(QUEUE_STORAGE_KEY)
    $queuedPromptsBySession.set({})
  })

  it('moves entries from a dead runtime key onto the live one', () => {
    enqueueQueuedPrompt('rt-old', { attachments: [], text: 'stranded' })

    expect(migrateQueuedPrompts('rt-old', 'rt-new')).toBe(true)
    expect(getQueuedPrompts('rt-old')).toEqual([])
    expect(getQueuedPrompts('rt-new').map(e => e.text)).toEqual(['stranded'])
    // The dead key is dropped from the store entirely.
    expect($queuedPromptsBySession.get()['rt-old']).toBeUndefined()
  })

  it('appends after existing target entries (FIFO preserved)', () => {
    enqueueQueuedPrompt('rt-new', { attachments: [], text: 'already here' })
    enqueueQueuedPrompt('rt-old', { attachments: [], text: 'migrated' })

    migrateQueuedPrompts('rt-old', 'rt-new')

    expect(getQueuedPrompts('rt-new').map(e => e.text)).toEqual(['already here', 'migrated'])
  })

  it('migrates from one fresh persisted snapshot without losing interleaved source or target writes', () => {
    const source = enqueueQueuedPrompt('rt-old', { attachments: [], text: 'source first' })!
    const staleTarget = enqueueQueuedPrompt('rt-new', { attachments: [], text: 'target removed elsewhere' })!
    const sourceExternal = { ...source, id: 'source-external', text: 'source added elsewhere' }
    const targetExternal = { ...staleTarget, id: 'target-external', text: 'target added elsewhere' }

    window.localStorage.setItem(
      QUEUE_STORAGE_KEY,
      JSON.stringify({ 'rt-old': [source, sourceExternal], 'rt-new': [targetExternal] })
    )

    expect(migrateQueuedPrompts('rt-old', 'rt-new')).toBe(true)
    expect(getQueuedPrompts('rt-old')).toEqual([])
    expect(getQueuedPrompts('rt-new').map(entry => entry.text)).toEqual([
      'target added elsewhere',
      'source first',
      'source added elsewhere'
    ])
  })

  it('is a no-op when source is empty or keys match', () => {
    expect(migrateQueuedPrompts('rt-old', 'rt-new')).toBe(false)
    expect(migrateQueuedPrompts('rt-x', 'rt-x')).toBe(false)
  })

  it('must not be used across sessions without a same-lineage guard (documents the leak)', () => {
    // ChatView used to call migrateQueuedPrompts(selectedA, routeKeyB) during the
    // route-ahead/store-lag window of a session switch. That re-homes A's queue
    // onto B so the idle ChatBar on B auto-drains it into the wrong conversation.
    // The guard lives in shouldMigrateComposerScope — this test locks the
    // underlying hazard so a future caller cannot treat migrate as free.
    enqueueQueuedPrompt('root-a', { attachments: [], text: 'belongs to A' })

    expect(migrateQueuedPrompts('root-a', 'root-b')).toBe(true)
    expect(getQueuedPrompts('root-a')).toEqual([])
    expect(getQueuedPrompts('root-b').map(e => e.text)).toEqual(['belongs to A'])
  })
})

describe('shouldAutoDrain', () => {
  it('drains whenever idle with a non-empty queue', () => {
    expect(shouldAutoDrain({ isBusy: false, queueLength: 1 })).toBe(true)
  })

  it('drains on mount/reconnect with no observed busy edge', () => {
    // The whole point of dropping the edge: a remount resets the busy ref, so an
    // edge-gated drain would strand the entry. Idle + non-empty must still fire.
    expect(shouldAutoDrain({ isBusy: false, queueLength: 2 })).toBe(true)
  })

  it('does not drain mid-turn', () => {
    expect(shouldAutoDrain({ isBusy: true, queueLength: 1 })).toBe(false)
  })

  it('does not drain an empty queue', () => {
    expect(shouldAutoDrain({ isBusy: false, queueLength: 0 })).toBe(false)
  })

  it('does not drain a parked queue, even when idle', () => {
    // The Stop/Esc settle edge: busy just flipped false but the user asked to
    // HALT — the park must hold the head back until they resume.
    expect(shouldAutoDrain({ isBusy: false, parked: true, queueLength: 1 })).toBe(false)
  })

  it('drains again once the park is lifted', () => {
    expect(shouldAutoDrain({ isBusy: false, parked: false, queueLength: 1 })).toBe(true)
  })
})

describe('parked queue sessions', () => {
  beforeEach(() => {
    window.localStorage.removeItem(QUEUE_STORAGE_KEY)
    $queuedPromptsBySession.set({})
    $parkedQueueSessions.set({})
  })

  it('parks only sessions with queued entries', () => {
    expect(parkQueuedPrompts(SESSION_KEY)).toBe(false)
    expect(isQueueParked(SESSION_KEY)).toBe(false)

    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'held back' })

    expect(parkQueuedPrompts(SESSION_KEY)).toBe(true)
    expect(isQueueParked(SESSION_KEY)).toBe(true)
  })

  it('parks a fresh persisted queue before the local storage event arrives', () => {
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'external queue' })
    $queuedPromptsBySession.set({})

    expect(parkQueuedPrompts(SESSION_KEY)).toBe(true)
    expect(isQueueParked(SESSION_KEY)).toBe(true)
  })

  it('unparks explicitly', () => {
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'held back' })
    parkQueuedPrompts(SESSION_KEY)

    unparkQueuedPrompts(SESSION_KEY)

    expect(isQueueParked(SESSION_KEY)).toBe(false)
  })

  it('queueing a fresh prompt lifts the park', () => {
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'held back' })
    parkQueuedPrompts(SESSION_KEY)

    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'new intent' })

    expect(isQueueParked(SESSION_KEY)).toBe(false)
  })

  it('emptying the queue drops the park', () => {
    const entry = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'held back' })
    parkQueuedPrompts(SESSION_KEY)

    removeQueuedPrompt(SESSION_KEY, entry!.id)

    expect(isQueueParked(SESSION_KEY)).toBe(false)
  })

  it('a park travels with migrated entries', () => {
    // A backend bounce right after Stop re-keys the queue; shedding the park
    // there would auto-send the exact prompts the user just halted.
    enqueueQueuedPrompt('rt-old', { attachments: [], text: 'held back' })
    parkQueuedPrompts('rt-old')

    migrateQueuedPrompts('rt-old', 'rt-new')

    expect(isQueueParked('rt-old')).toBe(false)
    expect(isQueueParked('rt-new')).toBe(true)
  })

  it('migration without a park does not invent one', () => {
    enqueueQueuedPrompt('rt-old', { attachments: [], text: 'flowing' })

    migrateQueuedPrompts('rt-old', 'rt-new')

    expect(isQueueParked('rt-new')).toBe(false)
  })
})
