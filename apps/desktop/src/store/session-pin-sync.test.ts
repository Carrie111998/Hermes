import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/types/hermes'

const patch = vi.fn<(id: string, pinned: boolean, profile?: null | string) => Promise<{ ok: boolean }>>(() =>
  Promise.resolve({ ok: true })
)

vi.mock('@/hermes', () => ({
  setSessionPinnedRemote: (id: string, pinned: boolean, profile?: null | string) => patch(id, pinned, profile)
}))

import { $pinnedSessionIds } from '@/store/layout'
import { $sessions } from '@/store/session'

import { watchSessionPins } from './session-pin-sync'

const row = (id: string, extra: Partial<SessionInfo> = {}): SessionInfo =>
  ({ id, message_count: 1, source: 'cli', started_at: 0, title: id, ...extra }) as SessionInfo

const flush = () => Promise.resolve()

beforeAll(() => {
  ;(globalThis as { window?: unknown }).window ??= {}
  ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = {}
  // Attach the listeners once — module state is process-global.
  watchSessionPins()
})

beforeEach(() => {
  $sessions.set([])
  $pinnedSessionIds.set([])
  patch.mockClear()
})

afterEach(() => {
  $sessions.set([])
  $pinnedSessionIds.set([])
})

describe('watchSessionPins', () => {
  it('mirrors a new pin as pinned=true with the row profile', async () => {
    $sessions.set([row('a', { profile: 'work' })])
    $pinnedSessionIds.set(['a'])
    await flush()

    expect(patch).toHaveBeenCalledWith('a', true, 'work')
  })

  it('keeps a new local pin while the loaded row still reports pinned=false', async () => {
    $sessions.set([row('stale-pin', { pinned: false, profile: 'work' })])
    $pinnedSessionIds.set(['stale-pin'])
    await flush()

    expect($pinnedSessionIds.get()).toContain('stale-pin')
    expect(patch).toHaveBeenCalledWith('stale-pin', true, 'work')
  })

  it('mirrors an unpin as pinned=false', async () => {
    $sessions.set([row('b')])
    $pinnedSessionIds.set(['b'])
    await flush()
    patch.mockClear()

    $pinnedSessionIds.set([])
    await flush()

    expect(patch).toHaveBeenCalledWith('b', false, undefined)
  })

  it('keeps a local unpin while the loaded row still reports pinned=true', async () => {
    $sessions.set([row('stale-unpin', { pinned: true, profile: 'work' })])
    await flush()
    patch.mockClear()

    $pinnedSessionIds.set([])
    await flush()

    expect($pinnedSessionIds.get()).not.toContain('stale-unpin')
    expect(patch).toHaveBeenCalledWith('stale-unpin', false, 'work')
  })

  it('defers a pin whose row is not loaded, then flushes once it appears', async () => {
    $pinnedSessionIds.set(['c'])
    await flush()
    // No row yet -> nothing sent.
    expect(patch).not.toHaveBeenCalled()

    $sessions.set([row('c', { profile: 'p2' })])
    await flush()

    expect(patch).toHaveBeenCalledWith('c', true, 'p2')
  })

  it('matches a pin id against the lineage root', async () => {
    // pin id is the lineage root; the live row carries it as _lineage_root_id.
    $sessions.set([row('tip', { _lineage_root_id: 'root' })])
    $pinnedSessionIds.set(['root'])
    await flush()

    expect(patch).toHaveBeenCalledWith('root', true, undefined)
  })

  it('does not re-PATCH an already-mirrored pin on unrelated session updates', async () => {
    $sessions.set([row('d')])
    $pinnedSessionIds.set(['d'])
    await flush()
    patch.mockClear()

    // A session-list refresh that doesn't change the pinned set.
    $sessions.set([row('d'), row('e')])
    await flush()

    expect(patch).not.toHaveBeenCalled()
  })
})

describe('watchSessionPins remote pull', () => {
  it('adopts a pin another app made', async () => {
    $sessions.set([row('remote', { pinned: true })])
    await flush()

    expect($pinnedSessionIds.get()).toContain('remote')
  })

  it('adopts a remote pin on the durable lineage root, not the live tip', async () => {
    $sessions.set([row('tip', { _lineage_root_id: 'root', pinned: true })])
    await flush()

    expect($pinnedSessionIds.get()).toEqual(['root'])
  })

  it('does not echo an adopted pin back as a redundant write', async () => {
    $sessions.set([row('adopted', { pinned: true })])
    await flush()

    expect(patch).not.toHaveBeenCalled()
  })

  it('drops a local pin the server reports as unpinned', async () => {
    $pinnedSessionIds.set(['gone'])
    $sessions.set([row('gone', { pinned: true })])
    await flush()
    patch.mockClear()

    // Another app unpinned it; our next refresh carries the new truth.
    $sessions.set([row('gone', { pinned: false })])
    await flush()

    expect($pinnedSessionIds.get()).not.toContain('gone')
  })

  it('leaves the local set alone when the backend omits the flag', async () => {
    $pinnedSessionIds.set(['legacy'])
    // No `pinned` key at all — a runtime predating the column.
    $sessions.set([row('legacy')])
    await flush()

    expect($pinnedSessionIds.get()).toContain('legacy')
  })

  it('ignores a stale page that contradicts a write still in flight', async () => {
    let settle: (v: { ok: boolean }) => void = () => {}

    patch.mockImplementationOnce(() => new Promise(resolve => (settle = resolve)))

    $sessions.set([row('race')])
    $pinnedSessionIds.set(['race'])
    await flush()
    expect(patch).toHaveBeenCalledWith('race', true, undefined)

    // A list request issued before the PATCH lands still says pinned=false.
    // Honouring it would silently undo the pin the user just made.
    $sessions.set([row('race', { pinned: false })])
    await flush()

    expect($pinnedSessionIds.get()).toContain('race')

    // Once the write is acked, later server truth is honoured again.
    settle({ ok: true })
    await flush()
    await flush()

    $sessions.set([row('race', { pinned: false }), row('other')])
    await flush()

    expect($pinnedSessionIds.get()).not.toContain('race')
  })

  it('does not let an older successful pin clear a newer unpin guard', async () => {
    const settle: Array<(v: { ok: boolean }) => void> = []
    patch.mockImplementation(
      () => new Promise(resolve => settle.push(resolve))
    )

    $sessions.set([row('rapid', { pinned: false })])
    $pinnedSessionIds.set(['rapid'])
    await flush()
    $pinnedSessionIds.set([])
    await flush()

    expect(settle).toHaveLength(2)
    settle[0]?.({ ok: true })
    await flush()
    await flush()

    $sessions.set([row('rapid', { pinned: true })])
    await flush()
    const remainedUnpinned = !$pinnedSessionIds.get().includes('rapid')

    settle[1]?.({ ok: true })
    await flush()
    await flush()

    expect(remainedUnpinned).toBe(true)
  })

  it('does not let an older successful unpin clear a newer pin guard', async () => {
    const settle: Array<(v: { ok: boolean }) => void> = []
    patch.mockImplementation(
      () => new Promise(resolve => settle.push(resolve))
    )

    $sessions.set([row('rapid-repin', { pinned: true })])
    await flush()
    $pinnedSessionIds.set([])
    await flush()
    $pinnedSessionIds.set(['rapid-repin'])
    await flush()

    expect(settle).toHaveLength(2)
    settle[0]?.({ ok: true })
    await flush()
    await flush()

    $sessions.set([row('rapid-repin', { pinned: false })])
    await flush()
    const remainedPinned = $pinnedSessionIds.get().includes('rapid-repin')

    settle[1]?.({ ok: true })
    await flush()
    await flush()

    expect(remainedPinned).toBe(true)
  })

  it('does not let an older failed pin restart a newer unpin write', async () => {
    const settle: Array<{ reject: (error: Error) => void; resolve: (v: { ok: boolean }) => void }> = []
    patch.mockImplementation(
      () =>
        new Promise((resolve, reject) => {
          settle.push({ reject, resolve })
        })
    )

    $sessions.set([row('rapid-failure', { pinned: false })])
    $pinnedSessionIds.set(['rapid-failure'])
    await flush()
    $pinnedSessionIds.set([])
    await flush()

    expect(settle).toHaveLength(2)
    settle[0]?.reject(new Error('older write failed'))
    await flush()
    await flush()

    $sessions.set([row('rapid-failure', { pinned: true })])
    await flush()
    const remainedUnpinned = !$pinnedSessionIds.get().includes('rapid-failure')
    const writeCount = settle.length

    for (const deferred of settle.slice(1)) {
      deferred.resolve({ ok: true })
    }

    await flush()
    await flush()

    expect(remainedUnpinned).toBe(true)
    expect(writeCount).toBe(2)
  })

  it('retries the latest failed unpin without re-pinning locally', async () => {
    patch.mockRejectedValueOnce(new Error('unpin failed'))

    $sessions.set([row('retry-unpin', { pinned: true })])
    await flush()
    $pinnedSessionIds.set([])
    await flush()
    await flush()

    expect(patch).toHaveBeenCalledTimes(1)
    expect(patch).toHaveBeenLastCalledWith('retry-unpin', false, undefined)

    $sessions.set([row('retry-unpin', { pinned: true }), row('other')])
    await flush()
    await flush()

    expect(patch).toHaveBeenCalledTimes(2)
    expect(patch).toHaveBeenLastCalledWith('retry-unpin', false, undefined)
    expect($pinnedSessionIds.get()).not.toContain('retry-unpin')
  })

  it('retries the latest failed pin without unpinning locally', async () => {
    patch.mockRejectedValueOnce(new Error('pin failed'))

    $sessions.set([row('retry-pin', { pinned: false })])
    $pinnedSessionIds.set(['retry-pin'])
    await flush()
    await flush()

    expect(patch).toHaveBeenCalledTimes(1)
    expect(patch).toHaveBeenLastCalledWith('retry-pin', true, undefined)

    $sessions.set([row('retry-pin', { pinned: false }), row('other')])
    await flush()
    await flush()

    expect(patch).toHaveBeenCalledTimes(2)
    expect(patch).toHaveBeenLastCalledWith('retry-pin', true, undefined)
    expect($pinnedSessionIds.get()).toContain('retry-pin')
  })
})
