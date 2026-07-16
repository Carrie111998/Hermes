import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { ComposerAttachment } from './composer'
import {
  $queuedPromptsBySession,
  clearQueuedPrompts,
  dequeueQueuedPrompt,
  enqueueQueuedPrompt,
  getQueuedPrompts,
  migrateQueuedPrompts,
  promoteQueuedPrompt,
  QUEUE_STORAGE_KEY,
  QUEUE_TOMBSTONES_STORAGE_KEY,
  readFreshQueuedPrompts,
  removeQueuedPrompt,
  shouldAutoDrain,
  updateQueuedPrompt,
  updateQueuedPromptText,
  whenSessionDrainClaimReleased,
  withSessionDrainClaim
} from './composer-queue'
import {
  installFakeLocks,
  otherWindowWrites,
  persistedQueueTexts,
  remoteEntry,
  resetQueueStorage
} from './composer-queue-test-utils'

const SESSION_KEY = 'session-abc'

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
    resetQueueStorage()
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
    expect(window.localStorage.getItem(QUEUE_STORAGE_KEY)).toBeNull()
  })

  it('persists queue entries into local storage', () => {
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'persist me' })

    expect(persistedQueueTexts(SESSION_KEY)).toEqual(['persist me'])
  })
})

describe('migrateQueuedPrompts', () => {
  beforeEach(() => {
    resetQueueStorage()
  })

  it('moves entries from a dead runtime key onto the live one', async () => {
    enqueueQueuedPrompt('rt-old', { attachments: [], text: 'stranded' })

    await expect(migrateQueuedPrompts('rt-old', 'rt-new')).resolves.toBe(true)
    expect(getQueuedPrompts('rt-old')).toEqual([])
    expect(getQueuedPrompts('rt-new').map(e => e.text)).toEqual(['stranded'])
    // The dead key is dropped from the store entirely.
    expect($queuedPromptsBySession.get()['rt-old']).toBeUndefined()
  })

  it('appends after existing target entries (FIFO preserved)', async () => {
    enqueueQueuedPrompt('rt-new', { attachments: [], text: 'already here' })
    enqueueQueuedPrompt('rt-old', { attachments: [], text: 'migrated' })

    await migrateQueuedPrompts('rt-old', 'rt-new')

    expect(getQueuedPrompts('rt-new').map(e => e.text)).toEqual(['already here', 'migrated'])
  })

  it('is a no-op when source is empty or keys match', async () => {
    await expect(migrateQueuedPrompts('rt-old', 'rt-new')).resolves.toBe(false)
    await expect(migrateQueuedPrompts('rt-x', 'rt-x')).resolves.toBe(false)
  })

  it('waits for an in-flight drain on the source key before moving (#57516 follow-up)', async () => {
    const restore = installFakeLocks()

    try {
      const entry = enqueueQueuedPrompt('rt-old', { attachments: [], text: 'in flight' })

      // A drain holds rt-old's claim, mid-submit.
      let settleDrain!: () => void
      const drain = withSessionDrainClaim('rt-old', () => new Promise<void>(res => (settleDrain = res)))
      await Promise.resolve()

      // Migration must NOT move the in-flight entry while the claim is held.
      let migrated = false

      const migration = migrateQueuedPrompts('rt-old', 'rt-new').then(moved => {
        migrated = true

        return moved
      })

      await Promise.resolve()

      expect(migrated).toBe(false)
      expect(persistedQueueTexts('rt-old')).toEqual(['in flight'])

      // The drain finishes and removes its entry under the OLD key, exactly as
      // it would in the hook; only then does migration proceed (with nothing
      // left to move here).
      removeQueuedPrompt('rt-old', entry!.id)
      settleDrain()
      await drain

      await expect(migration).resolves.toBe(false)
      expect(persistedQueueTexts('rt-new')).toEqual([])
    } finally {
      restore()
    }
  })
})

describe('cross-window sync (#46732)', () => {
  beforeEach(() => {
    resetQueueStorage()
  })

  it('adopts another window’s write into the local atom', () => {
    otherWindowWrites(
      { 'session-remote': [remoteEntry('q-1', 'from window B')] },
      { fireEvent: true }
    )

    expect(getQueuedPrompts('session-remote').map(e => e.text)).toEqual(['from window B'])
  })

  it('does not clobber another window’s entries when saving its own', () => {
    // Window B enqueues for its session; this window then enqueues for a
    // different session. Both must survive in storage.
    otherWindowWrites({ 'session-b': [remoteEntry('q-b', 'B entry')] }, { fireEvent: true })
    enqueueQueuedPrompt('session-a', { attachments: [], text: 'A entry' })

    expect(persistedQueueTexts('session-a')).toEqual(['A entry'])
    expect(persistedQueueTexts('session-b')).toEqual(['B entry'])
  })

  it('merges over live storage even without a storage event (same-frame race)', () => {
    // The `storage` event is asynchronous in real browsers; a write racing it
    // must still be preserved because mutations re-read storage at save time.
    otherWindowWrites({ 'session-b': [remoteEntry('q-b', 'unsynced B entry')] })

    enqueueQueuedPrompt('session-a', { attachments: [], text: 'A entry' })

    expect(persistedQueueTexts('session-a')).toEqual(['A entry'])
    expect(persistedQueueTexts('session-b')).toEqual(['unsynced B entry'])
  })

  it('drops entries locally once another window drains them', () => {
    enqueueQueuedPrompt('session-a', { attachments: [], text: 'about to be drained elsewhere' })

    // Window B drains session-a and persists the now-empty map.
    otherWindowWrites({}, { fireEvent: true })

    expect(getQueuedPrompts('session-a')).toEqual([])
    expect(dequeueQueuedPrompt('session-a')).toBeNull()
  })

  it('resyncs on full storage clear (event.key === null)', () => {
    enqueueQueuedPrompt('session-a', { attachments: [], text: 'entry' })

    window.localStorage.clear()
    window.dispatchEvent(new StorageEvent('storage', { key: null }))

    expect(getQueuedPrompts('session-a')).toEqual([])
  })

  it('ignores storage events for unrelated keys', () => {
    enqueueQueuedPrompt('session-a', { attachments: [], text: 'kept' })

    window.dispatchEvent(new StorageEvent('storage', { key: 'some.other.key', newValue: '"x"' }))

    expect(getQueuedPrompts('session-a').map(e => e.text)).toEqual(['kept'])
  })
})

describe('same-session cross-window races (#57516 review)', () => {
  beforeEach(() => {
    resetQueueStorage()
  })

  it('enqueue appends to the other window’s unsynced same-session write instead of clobbering it', () => {
    otherWindowWrites({ [SESSION_KEY]: [remoteEntry('q-b', 'B entry')] })

    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'local entry' })

    expect(persistedQueueTexts(SESSION_KEY)).toEqual(['B entry', 'local entry'])
    // The merge also lands in the local atom, not just storage.
    expect(getQueuedPrompts(SESSION_KEY).map(e => e.text)).toEqual(['B entry', 'local entry'])
  })

  it('update edits the fresh queue, preserving entries the local atom has not seen', () => {
    const mine = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'mine' })
    otherWindowWrites({ [SESSION_KEY]: [mine!, remoteEntry('q-b', 'B entry')] })

    expect(updateQueuedPromptText(SESSION_KEY, mine!.id, 'mine edited')).toBe(true)

    expect(persistedQueueTexts(SESSION_KEY)).toEqual(['mine edited', 'B entry'])
  })

  it('remove filters the fresh queue, preserving entries the local atom has not seen', () => {
    const mine = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'mine' })
    otherWindowWrites({ [SESSION_KEY]: [mine!, remoteEntry('q-b', 'B entry')] })

    expect(removeQueuedPrompt(SESSION_KEY, mine!.id)).toBe(true)

    expect(persistedQueueTexts(SESSION_KEY)).toEqual(['B entry'])
  })

  it('remove reports false for an entry another window already drained', () => {
    const mine = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'mine' })
    otherWindowWrites({})

    expect(removeQueuedPrompt(SESSION_KEY, mine!.id)).toBe(false)
  })

  it('dequeue takes the head of the fresh queue, not the stale atom’s head', () => {
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'already drained elsewhere' })
    otherWindowWrites({ [SESSION_KEY]: [remoteEntry('q-b', 'B entry')] })

    expect(dequeueQueuedPrompt(SESSION_KEY)?.text).toBe('B entry')
    expect(persistedQueueTexts(SESSION_KEY)).toEqual([])
  })

  it('promote reorders the fresh queue, preserving entries the local atom has not seen', () => {
    const first = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'first' })
    const second = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'second' })
    otherWindowWrites({ [SESSION_KEY]: [first!, second!, remoteEntry('q-b', 'B entry')] })

    expect(promoteQueuedPrompt(SESSION_KEY, second!.id)).toBe(true)

    expect(persistedQueueTexts(SESSION_KEY)).toEqual(['second', 'first', 'B entry'])
  })

  it('migrate moves the fresh source queue and appends to the fresh target queue', async () => {
    enqueueQueuedPrompt('rt-old', { attachments: [], text: 'seen locally' })
    otherWindowWrites({
      'rt-old': [remoteEntry('q-o', 'unsynced source entry')],
      'rt-new': [remoteEntry('q-n', 'unsynced target entry')]
    })

    await expect(migrateQueuedPrompts('rt-old', 'rt-new')).resolves.toBe(true)

    expect(persistedQueueTexts('rt-old')).toEqual([])
    expect(persistedQueueTexts('rt-new')).toEqual(['unsynced target entry', 'unsynced source entry'])
  })

  it('readFreshQueuedPrompts bypasses the stale atom', () => {
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'stale' })
    otherWindowWrites({ [SESSION_KEY]: [remoteEntry('q-b', 'B entry')] })

    expect(getQueuedPrompts(SESSION_KEY).map(e => e.text)).toEqual(['stale'])
    expect(readFreshQueuedPrompts(SESSION_KEY).map(e => e.text)).toEqual(['B entry'])
  })
})

describe('removal tombstones (#57516 follow-up)', () => {
  beforeEach(() => {
    resetQueueStorage()
  })

  it('a stale concurrent save cannot resurrect a removed entry into reads', () => {
    const sent = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'sent already' })
    removeQueuedPrompt(SESSION_KEY, sent!.id)

    // Window B loaded the map before the removal and saves after it: the raw
    // map now contains the removed entry again, plus B's own new entry.
    otherWindowWrites({ [SESSION_KEY]: [sent!, remoteEntry('q-b', 'B entry')] })

    // Neither drains nor displays may ever see the resurrected entry.
    expect(readFreshQueuedPrompts(SESSION_KEY).map(e => e.text)).toEqual(['B entry'])

    window.dispatchEvent(
      new StorageEvent('storage', { key: QUEUE_STORAGE_KEY, newValue: window.localStorage.getItem(QUEUE_STORAGE_KEY) })
    )
    expect(getQueuedPrompts(SESSION_KEY).map(e => e.text)).toEqual(['B entry'])

    // The next write purges it from storage too.
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'local entry' })
    expect(persistedQueueTexts(SESSION_KEY)).toEqual(['B entry', 'local entry'])
  })

  it('cleared entries cannot be resurrected either', () => {
    const a = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'cleared A' })
    const b = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'cleared B' })

    clearQueuedPrompts(SESSION_KEY)
    otherWindowWrites({ [SESSION_KEY]: [a!, b!] })

    expect(readFreshQueuedPrompts(SESSION_KEY)).toEqual([])
  })

  it('does not affect entries that were never removed', () => {
    const kept = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'kept' })
    const removed = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'removed' })

    removeQueuedPrompt(SESSION_KEY, removed!.id)

    expect(readFreshQueuedPrompts(SESSION_KEY).map(e => e.text)).toEqual(['kept'])
    expect(kept).not.toBeNull()
  })
})

describe('storage-failure in-memory fallback', () => {
  beforeEach(() => {
    resetQueueStorage()
  })

  const breakStorage = () => {
    const quota = () => {
      throw new DOMException('quota exceeded', 'QuotaExceededError')
    }

    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(quota)
    const removeItem = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(quota)

    return () => {
      setItem.mockRestore()
      removeItem.mockRestore()
    }
  }

  it('keeps queueing, reading, and removing in-memory while saves fail', () => {
    const restore = breakStorage()

    try {
      const a = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'first (unsaved)' })
      const b = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'second (unsaved)' })

      expect(a).not.toBeNull()
      expect(b).not.toBeNull()

      // Both entries survive in the atom AND in the fresh read drains use —
      // the second enqueue must not rebuild from empty storage and drop the first.
      expect(getQueuedPrompts(SESSION_KEY).map(e => e.text)).toEqual(['first (unsaved)', 'second (unsaved)'])
      expect(readFreshQueuedPrompts(SESSION_KEY).map(e => e.text)).toEqual(['first (unsaved)', 'second (unsaved)'])

      // In-memory entries stay mutable: remove works without storage — even
      // its tombstone lives in the in-memory overlay.
      expect(removeQueuedPrompt(SESSION_KEY, a!.id)).toBe(true)
      expect(readFreshQueuedPrompts(SESSION_KEY).map(e => e.text)).toEqual(['second (unsaved)'])

      // Clearing a session with in-memory-only entries works too.
      clearQueuedPrompts(SESSION_KEY)
      expect(getQueuedPrompts(SESSION_KEY)).toEqual([])
    } finally {
      restore()
    }
  })

  it('recovers exactly on the first successful save — not before, not never', () => {
    const restore = breakStorage()
    const survivorText = 'survivor (unsaved)'

    try {
      enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: survivorText })
    } finally {
      restore()
    }

    // Storage works again, but no successful save has happened yet: fresh
    // reads must STILL serve the atom, or the in-memory survivor would vanish.
    // (Pins that persistFailed stays set until an actual save succeeds.)
    otherWindowWrites({ [SESSION_KEY]: [remoteEntry('q-b', 'from healthy window')] })
    expect(readFreshQueuedPrompts(SESSION_KEY).map(e => e.text)).toEqual([survivorText])

    // The first mutation persists the atom-derived state (this window's truth
    // reconciles over storage) and clears the flag…
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'persisted again' })
    expect(persistedQueueTexts(SESSION_KEY)).toEqual([survivorText, 'persisted again'])

    // …after which fresh reads follow STORAGE again. (Pins that the
    // persistFailed reset in save()'s success path actually runs.)
    otherWindowWrites({ [SESSION_KEY]: [remoteEntry('q-c', 'storage truth')] })
    expect(readFreshQueuedPrompts(SESSION_KEY).map(e => e.text)).toEqual(['storage truth'])
  })

  it('ignores cross-window storage events while persistence is broken', () => {
    const restore = breakStorage()

    try {
      enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'memory only' })

      // Another window's write lands as a storage event; adopting it would
      // delete the in-memory-only entry.
      window.dispatchEvent(new StorageEvent('storage', { key: QUEUE_STORAGE_KEY, newValue: '{}' }))

      expect(getQueuedPrompts(SESSION_KEY).map(e => e.text)).toEqual(['memory only'])
      expect(readFreshQueuedPrompts(SESSION_KEY).map(e => e.text)).toEqual(['memory only'])
    } finally {
      restore()
    }
  })
})

describe('corrupt storage sanitization', () => {
  beforeEach(() => {
    resetQueueStorage()
  })

  it('drops malformed session values and entries instead of throwing', () => {
    window.localStorage.setItem(
      QUEUE_STORAGE_KEY,
      JSON.stringify({
        'session-bad': 'not an array',
        'session-mixed': [remoteEntry('q-ok', 'ok'), 'garbage', { id: 42 }, null, 7],
        'session-num': 12
      })
    )
    window.dispatchEvent(new StorageEvent('storage', { key: QUEUE_STORAGE_KEY, newValue: 'x' }))

    expect(getQueuedPrompts('session-mixed').map(e => e.text)).toEqual(['ok'])
    expect(getQueuedPrompts('session-bad')).toEqual([])
    expect(readFreshQueuedPrompts('session-num')).toEqual([])

    // Mutations over the corrupt map still work and persist a clean shape.
    enqueueQueuedPrompt('session-bad', { attachments: [], text: 'recovered' })
    expect(persistedQueueTexts('session-bad')).toEqual(['recovered'])
  })

  it('treats unparseable storage as unreadable, not as a crash', () => {
    window.localStorage.setItem(QUEUE_STORAGE_KEY, '{"truncated": ')
    window.dispatchEvent(new StorageEvent('storage', { key: QUEUE_STORAGE_KEY, newValue: 'x' }))

    expect(readFreshQueuedPrompts(SESSION_KEY)).toEqual([])
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'fresh start' })
    expect(getQueuedPrompts(SESSION_KEY).map(e => e.text)).toEqual(['fresh start'])
  })
})

describe('tombstone lifetime', () => {
  beforeEach(() => {
    resetQueueStorage()
  })

  it('purges a resurrected ghost from storage on the next storage event', () => {
    const sent = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'sent already' })
    removeQueuedPrompt(SESSION_KEY, sent!.id)

    // The stale save resurrects it — with the event, as another window's
    // write would. The ghost must leave STORAGE, not just be filtered.
    otherWindowWrites({ [SESSION_KEY]: [sent!, remoteEntry('q-b', 'B entry')] }, { fireEvent: true })

    expect(persistedQueueTexts(SESSION_KEY)).toEqual(['B entry'])
    expect(getQueuedPrompts(SESSION_KEY).map(e => e.text)).toEqual(['B entry'])
  })

  it('keeps a tombstone effective throughout the TTL and sheds it after', () => {
    vi.useFakeTimers({ now: new Date('2026-07-16T00:00:00Z'), toFake: ['Date'] })

    try {
      const sent = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'sent once' })
      removeQueuedPrompt(SESSION_KEY, sent!.id)

      otherWindowWrites({ [SESSION_KEY]: [sent!] }) // resurrected, no event

      expect(readFreshQueuedPrompts(SESSION_KEY)).toEqual([])

      vi.setSystemTime(new Date('2026-07-16T12:00:00Z')) // 12h — inside the 24h TTL
      expect(readFreshQueuedPrompts(SESSION_KEY)).toEqual([])

      vi.setSystemTime(new Date('2026-07-17T00:00:01Z')) // past the TTL
      // Documented shedding behavior: after the (deliberately long) TTL an
      // un-purged raw copy becomes visible again. Ghosts are normally purged
      // within one storage event (test above), far inside the TTL.
      expect(readFreshQueuedPrompts(SESSION_KEY).map(e => e.text)).toEqual(['sent once'])
    } finally {
      vi.useRealTimers()
    }
  })

  it('clamps future-dated tombstone garbage so it cannot evict genuine tombstones', () => {
    // Corruption scenario: the sidecar is stuffed with future-dated stamps.
    // Unclamped, they would never expire, always sort newest, permanently
    // occupy the cap, and evict every genuine tombstone at the next prune.
    const garbage: Record<string, number> = {}

    for (let i = 0; i < 70; i++) {
      garbage[`junk-${i}`] = Date.parse('2199-01-01T00:00:00Z') + i * 1_000
    }

    window.localStorage.setItem(QUEUE_TOMBSTONES_STORAGE_KEY, JSON.stringify(garbage))

    const a = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'genuinely removed A' })
    const b = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'genuinely removed B' })
    removeQueuedPrompt(SESSION_KEY, a!.id)
    removeQueuedPrompt(SESSION_KEY, b!.id) // this prune must NOT evict A's tombstone

    otherWindowWrites({ [SESSION_KEY]: [a!, b!] }) // stale save resurrects both

    expect(readFreshQueuedPrompts(SESSION_KEY)).toEqual([])
  })

  it('evicts the oldest tombstones beyond the cap, keeping the newest effective', () => {
    vi.useFakeTimers({ now: new Date('2026-07-16T00:00:00Z'), toFake: ['Date'] })

    try {
      const first = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'oldest removal' })
      removeQueuedPrompt(SESSION_KEY, first!.id)

      let last = first

      for (let i = 0; i < 70; i++) {
        vi.setSystemTime(new Date(Date.parse('2026-07-16T00:00:00Z') + (i + 1) * 1_000))
        const entry = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: `filler ${i}` })
        removeQueuedPrompt(SESSION_KEY, entry!.id)
        last = entry
      }

      // The oldest tombstone fell off the cap: its entry resurrects. The
      // newest is still effective.
      otherWindowWrites({ [SESSION_KEY]: [first!, last!] })

      expect(readFreshQueuedPrompts(SESSION_KEY).map(e => e.text)).toEqual(['oldest removal'])
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('withSessionDrainClaim', () => {
  beforeEach(() => {
    resetQueueStorage()
  })

  it('runs the task directly when Web Locks are unavailable (single-window env)', async () => {
    expect('locks' in window.navigator).toBe(false)

    await expect(withSessionDrainClaim('session-x', () => Promise.resolve('ran'))).resolves.toBe('ran')
  })

  it('resolves null without running the task for an unusable session key', async () => {
    const task = vi.fn(() => Promise.resolve('ran'))

    await expect(withSessionDrainClaim('   ', task)).resolves.toBeNull()
    expect(task).not.toHaveBeenCalled()
  })

  it('skips (null) while another window holds the claim, and reacquires after release', async () => {
    const restore = installFakeLocks()

    try {
      let releaseWinner!: (value: string) => void
      const winner = withSessionDrainClaim('session-x', () => new Promise<string>(res => (releaseWinner = res)))

      const loserTask = vi.fn(() => Promise.resolve('loser ran'))
      await expect(withSessionDrainClaim('session-x', loserTask)).resolves.toBeNull()
      expect(loserTask).not.toHaveBeenCalled()

      // Claims are per session: a different session drains concurrently.
      await expect(withSessionDrainClaim('session-y', () => Promise.resolve('other session'))).resolves.toBe(
        'other session'
      )

      releaseWinner('winner ran')
      await expect(winner).resolves.toBe('winner ran')

      // Released claim is available again.
      await expect(withSessionDrainClaim('session-x', () => Promise.resolve('second ran'))).resolves.toBe('second ran')
    } finally {
      restore()
    }
  })

  it('wait: true queues behind the holder and runs after release', async () => {
    const restore = installFakeLocks()

    try {
      let releaseWinner!: () => void
      const winner = withSessionDrainClaim('session-x', () => new Promise<void>(res => (releaseWinner = res)))
      await Promise.resolve()

      const order: string[] = []

      const waiter = withSessionDrainClaim(
        'session-x',
        async () => {
          order.push('waiter ran')

          return 'waited'
        },
        { wait: true }
      )

      await Promise.resolve()

      expect(order).toEqual([])

      releaseWinner()
      await winner

      await expect(waiter).resolves.toBe('waited')
      expect(order).toEqual(['waiter ran'])
    } finally {
      restore()
    }
  })

  it('wait with timeoutMs resolves null (not a rejection) when the holder outlasts it', async () => {
    const restore = installFakeLocks()

    try {
      let releaseWinner!: () => void
      const winner = withSessionDrainClaim('session-x', () => new Promise<void>(res => (releaseWinner = res)))
      await Promise.resolve()

      const task = vi.fn(() => Promise.resolve('late'))
      await expect(withSessionDrainClaim('session-x', task, { timeoutMs: 20, wait: true })).resolves.toBeNull()
      expect(task).not.toHaveBeenCalled()

      releaseWinner()
      await winner
    } finally {
      restore()
    }
  })
})

describe('whenSessionDrainClaimReleased', () => {
  beforeEach(() => {
    resetQueueStorage()
  })

  it('reports false without Web Locks (no wait happened) and true for a free claim', async () => {
    await expect(whenSessionDrainClaimReleased('session-x')).resolves.toBe(false)

    const restore = installFakeLocks()

    try {
      await expect(whenSessionDrainClaimReleased('session-x')).resolves.toBe(true)
    } finally {
      restore()
    }
  })

  it('resolves only after the current holder releases — including a failed winner', async () => {
    const restore = installFakeLocks()

    try {
      let failWinner!: (error: Error) => void

      const winner = withSessionDrainClaim(
        'session-x',
        () => new Promise<never>((_resolve, reject) => (failWinner = reject))
      ).catch(() => 'winner failed')

      await Promise.resolve()

      let released = false

      const waiter = whenSessionDrainClaimReleased('session-x').then(() => {
        released = true
      })

      await Promise.resolve()

      expect(released).toBe(false)

      // The winner FAILS (rejected submit / crash analogue): it writes nothing,
      // but the claim release alone must wake the waiter.
      failWinner(new Error('submit rejected'))
      await winner
      await waiter

      expect(released).toBe(true)
    } finally {
      restore()
    }
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
})
