import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesConnection } from '@/global'
import type { SessionInfo } from '@/types/hermes'

const patch = vi.fn<(id: string, pinned: boolean, profile?: null | string) => Promise<{ ok: boolean }>>(() =>
  Promise.resolve({ ok: true })
)

vi.mock('@/hermes', () => ({
  setSessionPinnedRemote: (id: string, pinned: boolean, profile?: null | string) => patch(id, pinned, profile)
}))

import { $pinnedSessionIds } from '@/store/layout'
import { $sessions, setConnection } from '@/store/session'
import { legacyPinnedSessionIds, pinnedSessionScopeInitialized } from '@/store/session-pins'

import { watchSessionPins } from './session-pin-sync'

const row = (id: string, extra: Partial<SessionInfo> = {}): SessionInfo =>
  ({ id, message_count: 1, source: 'cli', started_at: 0, title: id, ...extra }) as SessionInfo

const localConnection = { baseUrl: '', mode: 'local', profile: 'default' } as HermesConnection

const remoteConnection = (baseUrl: string): HermesConnection =>
  ({ baseUrl, mode: 'remote', profile: 'default', remoteKind: 'url' }) as HermesConnection

const flush = () => Promise.resolve()

beforeAll(() => {
  ;(globalThis as { window?: unknown }).window ??= {}
  ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = {}
  // Attach the listeners once — module state is process-global.
  watchSessionPins()
})

beforeEach(() => {
  setConnection(null)
  window.localStorage.clear()
  $sessions.set([])
  $pinnedSessionIds.set([])
  patch.mockClear()
  setConnection(localConnection)
})

afterEach(() => {
  setConnection(null)
  $sessions.set([])
  $pinnedSessionIds.set([])
  window.localStorage.clear()
})

describe('watchSessionPins', () => {
  it('mirrors a new pin as pinned=true with the row profile', async () => {
    $sessions.set([row('a', { profile: 'work' })])
    $pinnedSessionIds.set(['a'])
    await flush()

    expect(patch).toHaveBeenCalledWith('a', true, 'work')
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

  it('does not revert a fresh local pin while the loaded row is still stale (#74570)', async () => {
    // The row is already loaded and says pinned=false when the user pins.
    // The pin listener fires reconcile synchronously — before any PATCH — and
    // the stale row must not win over the local intent.
    $sessions.set([row('fresh', { pinned: false })])
    await flush()
    patch.mockClear()

    $pinnedSessionIds.set(['fresh'])
    await flush()

    expect($pinnedSessionIds.get()).toContain('fresh')
    expect(patch).toHaveBeenCalledWith('fresh', true, undefined)
  })

  it('does not revert a fresh local unpin while the loaded row still says pinned (#74570)', async () => {
    // Adopt a server-side pin first, so it's held locally and mirrored.
    $sessions.set([row('sticky', { pinned: true })])
    await flush()
    expect($pinnedSessionIds.get()).toContain('sticky')
    patch.mockClear()

    // User unpins while the loaded row still says pinned=true.
    $pinnedSessionIds.set([])
    await flush()

    expect($pinnedSessionIds.get()).not.toContain('sticky')
    expect(patch).toHaveBeenCalledWith('sticky', false, undefined)
  })

  it('keeps a deferred pin (row not yet loaded) when a stale page finally arrives', async () => {
    $pinnedSessionIds.set(['deferred'])
    await flush()
    expect(patch).not.toHaveBeenCalled()

    // The page that loads the row still predates our intent.
    $sessions.set([row('deferred', { pinned: false })])
    await flush()

    expect($pinnedSessionIds.get()).toContain('deferred')
    expect(patch).toHaveBeenCalledWith('deferred', true, undefined)
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
})

describe('watchSessionPins legacy migration', () => {
  it('imports only legacy pins that belong to the active connection', async () => {
    setConnection(null)
    window.localStorage.setItem('hermes.desktop.pinnedSessions', JSON.stringify(['local-pin', 'foreign-pin']))
    setConnection(localConnection)

    expect(window.localStorage.getItem('hermes.desktop.pinnedSessions.v2.local%3Adefault')).toBeNull()
    expect(pinnedSessionScopeInitialized()).toBe(false)
    expect(legacyPinnedSessionIds()).toEqual(['local-pin', 'foreign-pin'])

    $sessions.set([row('local-pin')])
    await flush()

    expect(pinnedSessionScopeInitialized()).toBe(true)
    expect($pinnedSessionIds.get()).toEqual(['local-pin'])
  })

  it('does not let legacy pins override a modern backend', async () => {
    setConnection(null)
    window.localStorage.setItem('hermes.desktop.pinnedSessions', JSON.stringify(['legacy-pin']))
    setConnection(localConnection)

    $sessions.set([row('legacy-pin', { pinned: false }), row('server-pin', { pinned: true })])
    await flush()

    expect($pinnedSessionIds.get()).toEqual(['server-pin'])
    expect(patch).not.toHaveBeenCalled()
  })

  it('does not let a scoped cache override a modern backend on reconnect', async () => {
    setConnection(null)
    window.localStorage.setItem('hermes.desktop.pinnedSessions.v2.local%3Adefault', JSON.stringify(['stale-local-pin']))
    setConnection(localConnection)

    $sessions.set([row('stale-local-pin', { pinned: false }), row('server-pin', { pinned: true })])
    await flush()

    expect($pinnedSessionIds.get()).toEqual(['server-pin'])
    expect(patch).not.toHaveBeenCalled()
  })
})

describe('watchSessionPins connection isolation', () => {
  it('does not let an earlier gateway write clear the active gateway write guard', async () => {
    let settleGatewayA: (value: { ok: boolean }) => void = () => {}

    let settleGatewayB: (value: { ok: boolean }) => void = () => {}

    patch
      .mockImplementationOnce(() => new Promise(resolve => (settleGatewayA = resolve)))
      .mockImplementationOnce(() => new Promise(resolve => (settleGatewayB = resolve)))

    setConnection(remoteConnection('https://gateway-a.example.test'))
    $sessions.set([row('shared', { pinned: false })])
    $pinnedSessionIds.set(['shared'])
    await flush()

    setConnection(remoteConnection('https://gateway-b.example.test'))
    $sessions.set([row('shared', { pinned: false })])
    $pinnedSessionIds.set(['shared'])
    await flush()
    expect(patch).toHaveBeenCalledTimes(2)

    // Gateway A responds after B has started its own write for the same id.
    // Its completion must not clear B's in-flight guard.
    settleGatewayA({ ok: true })
    await flush()
    await flush()

    $sessions.set([row('shared', { pinned: false }), row('other')])
    await flush()

    expect($pinnedSessionIds.get()).toContain('shared')

    settleGatewayB({ ok: true })
    await flush()
  })

  it('does not let an earlier gateway failure enqueue work on the active gateway', async () => {
    let rejectGatewayA: (error: Error) => void = () => {}

    patch.mockImplementationOnce(() => new Promise((_resolve, reject) => (rejectGatewayA = reject)))

    setConnection(remoteConnection('https://gateway-a.example.test'))
    $sessions.set([row('shared', { pinned: false })])
    $pinnedSessionIds.set(['shared'])
    await flush()

    setConnection(remoteConnection('https://gateway-b.example.test'))
    $sessions.set([row('shared', { pinned: true })])
    await flush()
    expect($pinnedSessionIds.get()).toContain('shared')
    patch.mockClear()

    rejectGatewayA(new Error('gateway A disconnected'))
    await flush()
    await flush()

    $sessions.set([row('shared', { pinned: true }), row('other')])
    await flush()

    expect($pinnedSessionIds.get()).toContain('shared')
    expect(patch).not.toHaveBeenCalled()
  })
})
