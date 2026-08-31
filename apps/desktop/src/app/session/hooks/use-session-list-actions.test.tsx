import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo, SidebarSessionsRequest, SidebarSessionsResponse } from '@/hermes'
import { $sessionsLimit, resetSessionsLimit, SIDEBAR_SESSIONS_INITIAL_LIMIT } from '@/store/layout'
import { $showAllProfiles, setShowAllProfiles } from '@/store/profile'
import {
  $cronSessions,
  $messagingSessions,
  $sessions,
  $sessionsLoading,
  setCronSessions,
  setMessagingSessions,
  setSessions,
  setSessionsLoading
} from '@/store/session'

import { useSessionListActions } from './use-session-list-actions'

// Sidebar refresh hygiene: a content-identical refresh (turn complete,
// cross-window broadcast, reconnect) must not replace $sessions' array
// identity — that identity is the dependency for every sidebar memo — and
// must not flicker the loading flag over an already-populated list.

const row = (id: string, over: Partial<SessionInfo> = {}): SessionInfo =>
  ({
    ended_at: null,
    id,
    input_tokens: 0,
    is_active: false,
    last_active: 1000,
    message_count: 3,
    model: 'm',
    output_tokens: 0,
    preview: 'hey',
    profile: 'default',
    source: 'desktop',
    started_at: 900,
    title: `Chat ${id}`,
    ...over
  }) as SessionInfo

// Batched sidebar response builder. `refreshSessions` now makes ONE
// listSidebarSessions call that returns all three slices, replacing the three
// separate listAllProfileSessions calls (each of which reopened every profile
// DB) — #66377-adjacent perf work from the desktop audit canvas.
const sidebar = (
  recents: { sessions: SessionInfo[]; total?: number; profile_totals?: Record<string, number> },
  cron: SessionInfo[] = [],
  messaging: SessionInfo[] = []
): SidebarSessionsResponse => ({
  recents: { sessions: recents.sessions, total: recents.total, profile_totals: recents.profile_totals },
  cron: { sessions: cron },
  messaging: { sessions: messaging, total: messaging.length }
})

const listSidebarSessions = vi.fn()
const listAllProfileSessions = vi.fn()
const notify = vi.fn()

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getCronJobs: vi.fn(async () => []),
  listAllProfileSessions: (...args: unknown[]) => listAllProfileSessions(...args),
  listSidebarSessions: (...args: unknown[]) => listSidebarSessions(...args)
}))

vi.mock('@/store/notifications', () => ({
  notify: (...args: unknown[]) => notify(...args),
  notifyError: vi.fn()
}))

beforeEach(() => {
  listSidebarSessions.mockReset()
  listAllProfileSessions.mockReset()
  notify.mockReset()
  setShowAllProfiles(false)
  $sessionsLimit.set(SIDEBAR_SESSIONS_INITIAL_LIMIT)
  setSessions([])
  setCronSessions([])
  setMessagingSessions([])
  setSessionsLoading(false)
})

afterEach(() => {
  resetSessionsLimit()
  setSessions([])
  setCronSessions([])
  setMessagingSessions([])
  setSessionsLoading(false)
  setShowAllProfiles(false)
})

// A concrete scope the backend doesn't recognize (profile deleted on disk, or
// a stray stored preference adopted at boot) used to come back as an empty
// recents slice with no error — the sidebar rendered permanently empty
// (observed 2026-08-31 with a ghost "diego" scope against 7,184 real
// sessions). The batched endpoint now echoes profile_matched; the hook must
// fall back to the all-profiles view and say so, once per ghost scope.
describe('ghost profile scope fallback', () => {
  const ghostResponse = (profile: string): SidebarSessionsResponse => ({
    recents: { sessions: [], total: 0, profile_totals: {}, profile, profile_matched: false },
    cron: { sessions: [] },
    messaging: { sessions: [], total: 0 }
  })

  it('flips to the all-profiles view and notifies once when the scope matches no profile', async () => {
    listSidebarSessions.mockResolvedValue(ghostResponse('diego'))

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'diego' }))

    await act(async () => {
      await result.current.refreshSessions()
    })

    expect($showAllProfiles.get()).toBe(true)
    expect(notify).toHaveBeenCalledTimes(1)
    expect($sessionsLoading.get()).toBe(false)

    // A repeat refresh against the same ghost scope re-asserts the fallback
    // without stacking another notification.
    setShowAllProfiles(false)

    await act(async () => {
      await result.current.refreshSessions()
    })

    expect($showAllProfiles.get()).toBe(true)
    expect(notify).toHaveBeenCalledTimes(1)
  })

  it('leaves a matched-but-empty scope and an indicator-less backend alone', async () => {
    listSidebarSessions.mockResolvedValue({
      recents: { sessions: [], total: 0, profile_totals: { work: 0 }, profile: 'work', profile_matched: true },
      cron: { sessions: [] },
      messaging: { sessions: [], total: 0 }
    } satisfies SidebarSessionsResponse)

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'work' }))

    await act(async () => {
      await result.current.refreshSessions()
    })

    expect($showAllProfiles.get()).toBe(false)
    expect(notify).not.toHaveBeenCalled()

    // Older backend / legacy per-slice fallback: no indicator field at all.
    listSidebarSessions.mockResolvedValue(sidebar({ sessions: [], total: 0 }))

    await act(async () => {
      await result.current.refreshSessions()
    })

    expect($showAllProfiles.get()).toBe(false)
    expect(notify).not.toHaveBeenCalled()
  })
})

describe('refreshSessions identity + loading hygiene', () => {
  it('keeps the previous $sessions array when the refresh is content-identical', async () => {
    const rows = [row('a'), row('b')]
    listSidebarSessions.mockResolvedValue(sidebar({ sessions: rows, total: 2 }))

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'default' }))

    await act(async () => {
      await result.current.refreshSessions()
    })

    const first = $sessions.get()
    expect(first.map(s => s.id)).toEqual(['a', 'b'])

    // Second refresh returns fresh (but equal) row objects, as the API does.
    listSidebarSessions.mockResolvedValue(sidebar({ sessions: [row('a'), row('b')], total: 2 }))

    await act(async () => {
      await result.current.refreshSessions()
    })

    expect($sessions.get()).toBe(first)
  })

  it('swaps the array when rows actually changed', async () => {
    listSidebarSessions.mockResolvedValue(sidebar({ sessions: [row('a')], total: 1, profile_totals: {} }))
    const { result } = renderHook(() => useSessionListActions({ profileScope: 'default' }))

    await act(async () => {
      await result.current.refreshSessions()
    })

    const first = $sessions.get()

    listSidebarSessions.mockResolvedValue(
      sidebar({ sessions: [row('a', { last_active: 2000, title: 'Renamed' })], total: 1, profile_totals: {} })
    )

    await act(async () => {
      await result.current.refreshSessions()
    })

    expect($sessions.get()).not.toBe(first)
    expect($sessions.get()[0].title).toBe('Renamed')
  })

  it('does not flicker the loading flag over a populated list', async () => {
    listSidebarSessions.mockResolvedValue(sidebar({ sessions: [row('a')], total: 1, profile_totals: {} }))
    const { result } = renderHook(() => useSessionListActions({ profileScope: 'default' }))

    await act(async () => {
      await result.current.refreshSessions()
    })

    const loadingStates: boolean[] = []
    const off = $sessionsLoading.subscribe(value => loadingStates.push(value))

    await act(async () => {
      await result.current.refreshSessions()
    })

    off()
    // Only the initial subscribe emission — no true/false churn per refresh.
    expect(loadingStates).toEqual([false])
  })

  it('still shows loading for the initial (empty-list) fetch', async () => {
    listSidebarSessions.mockResolvedValue(sidebar({ sessions: [row('a')], total: 1, profile_totals: {} }))
    const { result } = renderHook(() => useSessionListActions({ profileScope: 'default' }))

    const loadingStates: boolean[] = []
    const off = $sessionsLoading.subscribe(value => loadingStates.push(value))

    await act(async () => {
      await result.current.refreshSessions()
    })

    off()
    expect(loadingStates).toEqual([false, true, false])
  })
})

describe('refreshSessions batches slices into one request', () => {
  it('hydrates a cold 501-row total in bounded 0/500 pages, then skips stable rehydration', async () => {
    const rows = Array.from({ length: SIDEBAR_SESSIONS_INITIAL_LIMIT + 1 }, (_, index) =>
      row(`session-${index + 1}`, { last_active: SIDEBAR_SESSIONS_INITIAL_LIMIT - index })
    )

    listSidebarSessions.mockResolvedValue(
      sidebar({
        sessions: rows.slice(0, SIDEBAR_SESSIONS_INITIAL_LIMIT),
        total: rows.length,
        profile_totals: { default: rows.length }
      })
    )
    listAllProfileSessions.mockImplementation(
      (
        limit: number,
        _minMessages: number,
        _archived: string,
        _order: string,
        profile: string,
        filter: { offset?: number } = {}
      ) => {
        const offset = filter.offset ?? 0

        return Promise.resolve({
          limit,
          offset,
          profile_totals: { [profile]: rows.length },
          sessions: rows.slice(offset, offset + limit),
          total: rows.length
        })
      }
    )

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'default' }))

    await act(async () => {
      await result.current.refreshSessions()
    })

    expect(
      listAllProfileSessions.mock.calls.map(call => ({ limit: call[0], offset: call[5]?.offset ?? 0 }))
    ).toEqual([
      { limit: SIDEBAR_SESSIONS_INITIAL_LIMIT, offset: 0 },
      { limit: SIDEBAR_SESSIONS_INITIAL_LIMIT, offset: SIDEBAR_SESSIONS_INITIAL_LIMIT }
    ])
    expect(new Set($sessions.get().map(session => session.id)).size).toBe(rows.length)
    expect($sessions.get()).toHaveLength(rows.length)

    listSidebarSessions.mockClear()
    listAllProfileSessions.mockClear()

    await act(async () => {
      await result.current.refreshSessions()
    })

    expect(listSidebarSessions).toHaveBeenCalledTimes(1)
    expect(listSidebarSessions).toHaveBeenCalledWith(
      expect.objectContaining({ recentsLimit: SIDEBAR_SESSIONS_INITIAL_LIMIT })
    )
    expect(listAllProfileSessions).not.toHaveBeenCalled()
  })

  it('rehydrates a stable profile again after a soft gateway switch clears the session cache', async () => {
    const rows = Array.from({ length: SIDEBAR_SESSIONS_INITIAL_LIMIT + 1 }, (_, index) =>
      row(`session-${index + 1}`, { last_active: SIDEBAR_SESSIONS_INITIAL_LIMIT - index })
    )

    listSidebarSessions.mockResolvedValue(
      sidebar({
        sessions: rows.slice(0, SIDEBAR_SESSIONS_INITIAL_LIMIT),
        total: rows.length,
        profile_totals: { default: rows.length }
      })
    )
    listAllProfileSessions.mockImplementation(
      (
        limit: number,
        _minMessages: number,
        _archived: string,
        _order: string,
        profile: string,
        filter: { offset?: number } = {}
      ) => {
        const offset = filter.offset ?? 0

        return Promise.resolve({
          limit,
          offset,
          profile_totals: { [profile]: rows.length },
          sessions: rows.slice(offset, offset + limit),
          total: rows.length
        })
      }
    )

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'default' }))

    await act(async () => {
      await result.current.refreshSessions()
    })

    expect($sessions.get()).toHaveLength(rows.length)

    // Connection/mode apply wipes gateway-bound stores without remounting this
    // hook. The new backend may expose the same profile name and total.
    setSessions([])
    listAllProfileSessions.mockClear()

    await act(async () => {
      await result.current.refreshSessions()
    })

    expect(listAllProfileSessions.mock.calls.map(call => call[5]?.offset)).toEqual([
      0,
      SIDEBAR_SESSIONS_INITIAL_LIMIT
    ])
    expect($sessions.get()).toHaveLength(rows.length)
    expect($sessions.get().some(session => session.id === rows.at(-1)!.id)).toBe(true)
  })

  it('rehydrates a concrete profile when its explicit total changes', async () => {
    let rows = [row('session-1'), row('session-2')]

    listSidebarSessions.mockImplementation((request: SidebarSessionsRequest) =>
      Promise.resolve(
        sidebar({
          sessions: rows.slice(0, request.recentsLimit),
          total: rows.length,
          profile_totals: { default: rows.length }
        })
      )
    )
    listAllProfileSessions.mockImplementation(
      (
        limit: number,
        _minMessages: number,
        _archived: string,
        _order: string,
        profile: string,
        filter: { offset?: number } = {}
      ) => {
        const offset = filter.offset ?? 0

        return Promise.resolve({
          limit,
          offset,
          profile_totals: { [profile]: rows.length },
          sessions: rows.slice(offset, offset + limit),
          total: rows.length
        })
      }
    )

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'default' }))

    await act(async () => {
      await result.current.refreshSessions()
    })

    rows = [...rows, row('session-3')]

    await act(async () => {
      await result.current.refreshSessions()
    })

    expect(listAllProfileSessions).toHaveBeenCalledTimes(2)
    expect(listAllProfileSessions.mock.calls.map(call => call[5]?.offset)).toEqual([0, 0])
    expect($sessions.get().map(session => session.id)).toEqual(['session-1', 'session-2', 'session-3'])
  })

  it('does not hydrate or reconcile a fractional initial explicit total', async () => {
    const cached = [row('session-newest'), row('session-oldest', { last_active: 1 })]

    setSessions(cached)
    listSidebarSessions.mockResolvedValue(
      sidebar({ sessions: [cached[0]], total: 1.5, profile_totals: { default: 1.5 } })
    )
    listAllProfileSessions.mockResolvedValue({
      limit: SIDEBAR_SESSIONS_INITIAL_LIMIT,
      offset: 0,
      profile_totals: { default: 1 },
      sessions: [cached[0]],
      total: 1
    })

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'default' }))

    await act(async () => {
      await result.current.refreshSessions()
      await result.current.refreshSessions()
    })

    expect(listAllProfileSessions).not.toHaveBeenCalled()
    expect($sessions.get().some(session => session.id === 'session-oldest')).toBe(true)
    expect($sessions.get()).toHaveLength(cached.length)
  })

  it('retries hydration when a concrete page overshoots its advertised total', async () => {
    const cached = [row('session-newest'), row('session-oldest', { last_active: 1 })]
    const unexpected = row('session-unexpected', { last_active: 500 })

    setSessions(cached)
    listSidebarSessions.mockResolvedValue(
      sidebar({ sessions: [cached[0]], total: 1, profile_totals: { default: 1 } })
    )
    listAllProfileSessions.mockResolvedValue({
      limit: SIDEBAR_SESSIONS_INITIAL_LIMIT,
      offset: 0,
      profile_totals: { default: 1 },
      sessions: [cached[0], unexpected],
      total: 1
    })

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'default' }))

    await act(async () => {
      await result.current.refreshSessions()
      await result.current.refreshSessions()
    })

    // Overshoot is non-authoritative and is not memoized, so a later refresh
    // retries hydration without removing the cached tail.
    expect(listAllProfileSessions).toHaveBeenCalledTimes(2)
    expect($sessions.get().some(session => session.id === 'session-oldest')).toBe(true)
    expect($sessions.get()).toHaveLength(cached.length)
  })

  it('preserves the oldest cached row when a concrete page total drifts from the batched target', async () => {
    const cached = [row('session-newest'), row('session-oldest', { last_active: 1 })]
    const batchedTotal = 1

    setSessions(cached)
    listSidebarSessions.mockResolvedValue(
      sidebar({
        sessions: [cached[0]],
        total: batchedTotal,
        profile_totals: { default: batchedTotal }
      })
    )
    listAllProfileSessions.mockResolvedValue({
      limit: SIDEBAR_SESSIONS_INITIAL_LIMIT,
      offset: 0,
      profile_totals: { default: batchedTotal + 1 },
      sessions: [cached[0]],
      total: batchedTotal + 1
    })

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'default' }))

    await act(async () => {
      await result.current.refreshSessions()
      await result.current.refreshSessions()
    })

    // Total drift makes both concrete responses non-authoritative, so the
    // cached catalog remains additive and hydration is retried later.
    expect(listAllProfileSessions).toHaveBeenCalledTimes(2)
    expect($sessions.get().some(session => session.id === 'session-oldest')).toBe(true)
    expect($sessions.get()).toHaveLength(cached.length)
  })

  it.each(['throws', 'returns no progress'] as const)(
    'keeps the oldest cached row when a later hydration page %s',
    async failure => {
      const rows = Array.from({ length: SIDEBAR_SESSIONS_INITIAL_LIMIT + 1 }, (_, index) =>
        row(`session-${index + 1}`, { last_active: SIDEBAR_SESSIONS_INITIAL_LIMIT - index })
      )

      const oldestId = rows.at(-1)!.id

      setSessions(rows)
      listSidebarSessions.mockResolvedValue(
        sidebar({
          sessions: rows.slice(0, SIDEBAR_SESSIONS_INITIAL_LIMIT),
          total: rows.length,
          profile_totals: { default: rows.length }
        })
      )
      listAllProfileSessions.mockImplementation(
        (
          limit: number,
          _minMessages: number,
          _archived: string,
          _order: string,
          _profile: string,
          filter: { offset?: number } = {}
        ) => {
          const offset = filter.offset ?? 0

          if (offset === SIDEBAR_SESSIONS_INITIAL_LIMIT) {
            if (failure === 'throws') {
              return Promise.reject(new Error('page failed'))
            }

            return Promise.resolve({ limit, offset, sessions: [], total: rows.length })
          }

          return Promise.resolve({
            limit,
            offset,
            sessions: rows.slice(offset, offset + limit),
            total: rows.length
          })
        }
      )

      const { result } = renderHook(() => useSessionListActions({ profileScope: 'default' }))

      await act(async () => {
        await result.current.refreshSessions()
      })

      expect(listAllProfileSessions.mock.calls.map(call => call[5]?.offset)).toEqual([
        0,
        SIDEBAR_SESSIONS_INITIAL_LIMIT
      ])
      expect($sessions.get().some(session => session.id === oldestId)).toBe(true)
      expect($sessions.get()).toHaveLength(rows.length)
    }
  )

  it.each(['reports errors', 'returns an empty first page'] as const)(
    'preserves cached rows and does not reconcile when hydration %s',
    async failure => {
      const cached = [row('session-1'), row('session-oldest', { last_active: 1 })]

      setSessions(cached)
      listSidebarSessions.mockResolvedValue(
        sidebar({ sessions: [], total: cached.length, profile_totals: { default: cached.length } })
      )
      listAllProfileSessions.mockImplementation((limit: number, ...args: unknown[]) => {
        const filter = (args[4] as { offset?: number } | undefined) ?? {}
        const page = { limit, offset: filter.offset ?? 0, sessions: [], total: cached.length }

        return Promise.resolve(
          failure === 'reports errors' ? { ...page, errors: [{ error: 'remote failed', profile: 'default' }] } : page
        )
      })

      const { result } = renderHook(() => useSessionListActions({ profileScope: 'default' }))

      await act(async () => {
        await result.current.refreshSessions()
      })

      expect(listAllProfileSessions).toHaveBeenCalledTimes(1)
      expect($sessions.get()).toBe(cached)
      expect($sessions.get().map(session => session.id)).toContain('session-oldest')
    }
  )

  it('preserves cached rows and skips reconciliation for result errors with an empty first page', async () => {
    const cached = [
      row('remote-newest', { profile: 'remote' }),
      row('remote-oldest', { last_active: 1, profile: 'remote' })
    ]

    setSessions(cached)
    listSidebarSessions.mockResolvedValue({
      ...sidebar({ sessions: [], total: 0, profile_totals: { remote: 0 } }),
      errors: [{ error: 'remote failed', profile: 'remote' }]
    })

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'remote' }))

    await act(async () => {
      await result.current.refreshSessions()
    })

    expect(listAllProfileSessions).not.toHaveBeenCalled()
    expect($sessions.get()).toBe(cached)
  })

  it('does not invent an authoritative concrete total when profile_totals omits that profile', async () => {
    const cached = [
      row('remote-newest', { profile: 'remote' }),
      row('remote-oldest', { last_active: 1, profile: 'remote' })
    ]

    setSessions(cached)
    listSidebarSessions.mockResolvedValue(sidebar({ sessions: [], total: 0, profile_totals: {} }))
    listAllProfileSessions.mockResolvedValue({
      limit: SIDEBAR_SESSIONS_INITIAL_LIMIT,
      offset: 0,
      sessions: [],
      total: 0
    })

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'remote' }))

    await act(async () => {
      await result.current.refreshSessions()
    })

    expect(listAllProfileSessions).not.toHaveBeenCalled()
    expect($sessions.get()).toBe(cached)
  })

  it('makes a single sidebar call and distributes recents / cron / messaging', async () => {
    const recents = [row('a'), row('b')]
    const cron = [row('c1', { source: 'cron', title: 'nightly' })]
    const messaging = [row('m1', { source: 'telegram', title: 'tg chat' })]

    listSidebarSessions.mockResolvedValue(sidebar({ sessions: recents, total: 2 }, cron, messaging))

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'default' }))

    await act(async () => {
      await result.current.refreshSessions()
    })

    // One batched call, not three separate listAllProfileSessions reads.
    expect(listSidebarSessions).toHaveBeenCalledTimes(1)
    expect(listAllProfileSessions).not.toHaveBeenCalled()

    // Each slice landed in its own store.
    expect($sessions.get().map(s => s.id)).toEqual(['a', 'b'])
    expect($cronSessions.get().map(s => s.id)).toEqual(['c1'])
    expect($messagingSessions.get().map(s => s.id)).toEqual(['m1'])
  })

  it('forwards the active profile scope + section limits to the batched call', async () => {
    listSidebarSessions.mockResolvedValue(sidebar({ sessions: [], total: 0, profile_totals: {} }))
    const { result } = renderHook(() => useSessionListActions({ profileScope: 'work' }))

    await act(async () => {
      await result.current.refreshSessions()
    })

    expect(listSidebarSessions).toHaveBeenCalledWith(
      expect.objectContaining({
        recentsProfile: 'work',
        recentsExclude: expect.arrayContaining(['cron']),
        messagingExclude: expect.arrayContaining(['cron'])
      })
    )
  })

  it('scopes the cron-jobs fetch to the active profile (all → unified view)', async () => {
    const { getCronJobs } = await import('@/hermes')
    listSidebarSessions.mockResolvedValue(sidebar({ sessions: [], total: 0, profile_totals: {} }))

    const scoped = renderHook(() => useSessionListActions({ profileScope: 'work' }))

    await act(async () => {
      await scoped.result.current.refreshCronJobs()
    })

    expect(getCronJobs).toHaveBeenLastCalledWith('work')

    const unified = renderHook(() => useSessionListActions({ profileScope: '__all__' }))

    await act(async () => {
      await unified.result.current.refreshCronJobs()
    })

    expect(getCronJobs).toHaveBeenLastCalledWith('all')
  })
})

describe('loadMoreSessionsForProfile bounded paging', () => {
  it('requests one bounded next page and additively preserves the rank-51 row', async () => {
    const previous = Array.from({ length: 51 }, (_, index) => row(`session-${index + 1}`))
    const incoming = [row('session-52'), row('session-53')]
    const otherProfile = row('work-1', { profile: 'work' })

    setSessions([...previous, otherProfile])
    listAllProfileSessions.mockResolvedValue({
      limit: SIDEBAR_SESSIONS_INITIAL_LIMIT,
      offset: previous.length,
      profile_totals: { default: previous.length + incoming.length },
      sessions: incoming,
      total: previous.length + incoming.length
    })

    const { result } = renderHook(() => useSessionListActions({ profileScope: '__all__' }))

    await act(async () => {
      await result.current.loadMoreSessionsForProfile('default')
    })

    expect(listAllProfileSessions).toHaveBeenCalledTimes(1)
    expect(listAllProfileSessions).toHaveBeenCalledWith(
      SIDEBAR_SESSIONS_INITIAL_LIMIT,
      1,
      'exclude',
      'recent',
      'default',
      expect.objectContaining({ offset: previous.length })
    )

    const ids = $sessions.get().map(session => session.id)

    expect(ids).toContain('session-51')
    expect(ids).toContain('session-53')
    expect(ids).toContain(otherProfile.id)
    expect(new Set(ids).size).toBe(previous.length + incoming.length + 1)
  })
})