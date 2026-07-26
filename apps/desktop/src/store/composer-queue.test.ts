import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ComposerAttachment } from './composer'
import {
  $parkedQueueSessions,
  $queuedPromptsBySession,
  clearQueuedPrompts,
  dequeueQueuedPrompt,
  enqueueQueuedPrompt,
  getQueuedPrompts,
  isQueueParked,
  migrateQueuedPrompts,
  parkQueuedPrompts,
  promoteQueuedPrompt,
  removeQueuedPrompt,
  shouldAutoDrain,
  unparkQueuedPrompts,
  updateQueuedPrompt,
  updateQueuedPromptText
} from './composer-queue'

const SESSION_KEY = 'session-abc'
// PR3 persists under v2 (nested { version, byProfile, legacy }); v1 is read only
// as a migration source.
const QUEUE_STORAGE_KEY_V1 = 'hermes.desktop.composerQueue.v1'
const QUEUE_STORAGE_KEY_V2 = 'hermes.desktop.composerQueue.v2'

// The test runtime ships without a usable localStorage; install an in-memory one
// so the persistence/migration assertions actually exercise the v2 storage path.
function installLocalStorageMock(): void {
  const store = new Map<string, string>()
  const mock = {
    clear: () => store.clear(),
    getItem: (key: string) => store.get(key) ?? null,
    removeItem: (key: string) => void store.delete(key),
    setItem: (key: string, value: string) => void store.set(key, String(value))
  }

  try {
    Object.defineProperty(window, 'localStorage', { configurable: true, value: mock, writable: true })
  } catch {
    ;(window as { localStorage?: unknown }).localStorage = mock
  }
}

beforeAll(() => {
  installLocalStorageMock()
})

function clearQueueStorage(): void {
  window.localStorage.removeItem(QUEUE_STORAGE_KEY_V1)
  window.localStorage.removeItem(QUEUE_STORAGE_KEY_V2)
}

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
    clearQueueStorage()
    $queuedPromptsBySession.set({})
  })

  it('queues prompts in FIFO order', () => {
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'first' })
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'second' })

    expect(dequeueQueuedPrompt(SESSION_KEY)?.text).toBe('first')
    expect(dequeueQueuedPrompt(SESSION_KEY)?.text).toBe('second')
    expect(dequeueQueuedPrompt(SESSION_KEY)).toBeNull()
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

  it('clears queue state for a session', () => {
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [attachment('img-1', 'image')], text: 'queued' })

    clearQueuedPrompts(SESSION_KEY)

    expect(getQueuedPrompts(SESSION_KEY)).toEqual([])
    expect($queuedPromptsBySession.get()[SESSION_KEY]).toBeUndefined()
    // An empty queue removes the v2 key entirely.
    expect(window.localStorage.getItem(QUEUE_STORAGE_KEY_V2)).toBeNull()
  })

  it('persists queue entries into local storage (v2, profile-nested)', () => {
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'persist me' })

    const raw = window.localStorage.getItem(QUEUE_STORAGE_KEY_V2)
    expect(raw).toBeTruthy()

    // Foreground entries carry no profile, so they nest under `legacy` keyed by
    // storedSessionId; profile-targeted entries would nest under `byProfile`.
    const parsed = JSON.parse(String(raw)) as {
      version: number
      byProfile: Record<string, Record<string, { text: string }[]>>
      legacy: Record<string, { text: string }[]>
    }

    expect(parsed.version).toBe(2)
    expect(parsed.legacy[SESSION_KEY]?.[0]?.text).toBe('persist me')
  })
})

describe('migrateQueuedPrompts', () => {
  beforeEach(() => {
    window.localStorage.removeItem(QUEUE_STORAGE_KEY_V1)
    window.localStorage.removeItem(QUEUE_STORAGE_KEY_V2)
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

  it('is a no-op when source is empty or keys match', () => {
    expect(migrateQueuedPrompts('rt-old', 'rt-new')).toBe(false)
    expect(migrateQueuedPrompts('rt-x', 'rt-x')).toBe(false)
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
    window.localStorage.removeItem(QUEUE_STORAGE_KEY_V1)
    window.localStorage.removeItem(QUEUE_STORAGE_KEY_V2)
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

describe('v1 → v2 storage migration', () => {
  it('migrates flat v1 buckets into v2 legacy and removes the v1 key only after the write', async () => {
    // Seed a flat v1 queue as an older build would have left it.
    window.localStorage.setItem(
      QUEUE_STORAGE_KEY_V1,
      JSON.stringify({ 'stored-1': [{ attachments: [], id: 'q-1', queuedAt: 1, text: 'legacy prompt' }] })
    )
    window.localStorage.removeItem(QUEUE_STORAGE_KEY_V2)

    // A fresh module load runs the migration in load().
    vi.resetModules()
    const migrated = await import('./composer-queue')

    // The legacy bucket loads under its bare storedSessionId, profile-less.
    expect(migrated.getQueuedPrompts('stored-1').map(entry => entry.text)).toEqual(['legacy prompt'])

    // Persisted as nested v2 under `legacy`; the v1 key is gone.
    const v2 = JSON.parse(String(window.localStorage.getItem(QUEUE_STORAGE_KEY_V2))) as {
      legacy: Record<string, { text: string }[]>
      version: number
    }

    expect(v2.version).toBe(2)
    expect(v2.legacy['stored-1']?.[0]?.text).toBe('legacy prompt')
    expect(window.localStorage.getItem(QUEUE_STORAGE_KEY_V1)).toBeNull()
  })
})
