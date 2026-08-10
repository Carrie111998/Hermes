import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo, SidebarSessionsResponse } from '@/hermes'
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
  recents: { sessions: SessionInfo[]; profiles_truncated?: Record<string, boolean> },
  cron: SessionInfo[] = [],
  messaging: SessionInfo[] = [],
  errors: SidebarSessionsResponse['errors'] = []
): SidebarSessionsResponse => ({
  recents: { sessions: recents.sessions, profiles_truncated: recents.profiles_truncated },
  cron: { sessions: cron },
  messaging: { sessions: messaging },
  ...(errors.length ? { errors } : {})
})

const listSidebarSessions = vi.fn()
const listAllProfileSessions = vi.fn()

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getCronJobs: vi.fn(async () => []),
  listAllProfileSessions: (...args: unknown[]) => listAllProfileSessions(...args),
  listSidebarSessions: (...args: unknown[]) => listSidebarSessions(...args)
}))

// The refresh only reads the optimistic tombstone set; stub it so we don't pull
// the whole projects store (gateway / fs / git) into this hook's test.
const removed = vi.hoisted(() => ({ ids: new Set<string>() }))

vi.mock('@/store/projects', () => ({
  $removedSessionIds: { get: () => removed.ids }
}))

beforeEach(() => {
  listSidebarSessions.mockReset()
  listAllProfileSessions.mockReset()
  removed.ids = new Set()
  setSessions([])
  setCronSessions([])
  setMessagingSessions([])
  setSessionsLoading(false)
})

afterEach(() => {
  setSessions([])
  setCronSessions([])
  setMessagingSessions([])
  setSessionsLoading(false)
})

describe('refreshSessions identity + loading hygiene', () => {
  it('keeps the previous $sessions array when the refresh is content-identical', async () => {
    const rows = [row('a'), row('b')]
    listSidebarSessions.mockResolvedValue(sidebar({ sessions: rows }))

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'default' }))

    await act(async () => {
      await result.current.refreshSessions()
    })

    const first = $sessions.get()
    expect(first.map(s => s.id)).toEqual(['a', 'b'])

    // Second refresh returns fresh (but equal) row objects, as the API does.
    listSidebarSessions.mockResolvedValue(sidebar({ sessions: [row('a'), row('b')] }))

    await act(async () => {
      await result.current.refreshSessions()
    })

    expect($sessions.get()).toBe(first)
  })

  it('swaps the array when rows actually changed', async () => {
    listSidebarSessions.mockResolvedValue(sidebar({ sessions: [row('a')] }))
    const { result } = renderHook(() => useSessionListActions({ profileScope: 'default' }))

    await act(async () => {
      await result.current.refreshSessions()
    })

    const first = $sessions.get()

    listSidebarSessions.mockResolvedValue(sidebar({ sessions: [row('a', { last_active: 2000, title: 'Renamed' })] }))

    await act(async () => {
      await result.current.refreshSessions()
    })

    expect($sessions.get()).not.toBe(first)
    expect($sessions.get()[0].title).toBe('Renamed')
  })

  it('does not flicker the loading flag over a populated list', async () => {
    listSidebarSessions.mockResolvedValue(sidebar({ sessions: [row('a')] }))
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

  it('drops rows the user just deleted, even when the backend page still lists them', async () => {
    // A delete RPC is in flight: the row is tombstoned optimistically but the
    // batched refresh still carries it (and a lineage-tip variant). Both must be
    // filtered so the optimistic removal never flashes back.
    removed.ids = new Set(['b', 'root-c'])
    listSidebarSessions.mockResolvedValue(
      sidebar({
        sessions: [row('a'), row('b'), row('c', { _lineage_root_id: 'root-c' } as Partial<SessionInfo>)]
      })
    )

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'default' }))

    await act(async () => {
      await result.current.refreshSessions()
    })

    expect($sessions.get().map(s => s.id)).toEqual(['a'])
  })

  it('still shows loading for the initial (empty-list) fetch', async () => {
    listSidebarSessions.mockResolvedValue(sidebar({ sessions: [row('a')] }))
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
  it('makes a single sidebar call and distributes recents / cron / messaging', async () => {
    const recents = [row('a'), row('b')]
    const cron = [row('c1', { source: 'cron', title: 'nightly' })]
    const messaging = [row('m1', { source: 'telegram', title: 'tg chat' })]

    listSidebarSessions.mockResolvedValue(sidebar({ sessions: recents }, cron, messaging))

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
    listSidebarSessions.mockResolvedValue(sidebar({ sessions: [] }))
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
    listSidebarSessions.mockResolvedValue(sidebar({ sessions: [] }))

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

describe('refreshSessions preserves known rows when a profile read errors', () => {
  it('keeps rows of a profile whose read errored (merge, don\'t clobber)', async () => {
    // A resolved refresh can be partial: profile 'main' errored server-side so
    // its rows are missing from the page. They must survive — the failure is a
    // transient backend condition, not a deletion.
    setSessions([
      row('main-1', { profile: 'main' }),
      row('main-2', { profile: 'main' }),
      row('work-1', { profile: 'work' })
    ])
    listSidebarSessions.mockResolvedValue(
      sidebar({ sessions: [row('work-1', { profile: 'work' })] }, [], [], [
        { profile: 'main', error: 'database locked' }
      ])
    )

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'main' }))

    await act(async () => {
      await result.current.refreshSessions()
    })

    const ids = $sessions.get().map(s => s.id)
    expect(ids).toContain('main-1')
    expect(ids).toContain('main-2')
    expect(ids).toContain('work-1')
  })

  it('still evicts rows of healthy profiles the page omitted', async () => {
    // Granularity guard: only the errored profile's rows are protected. A
    // healthy profile's row that legitimately aged off the page must still go.
    setSessions([
      row('main-1', { profile: 'main' }),
      row('main-2', { profile: 'main' }),
      row('work-1', { profile: 'work' }),
      row('work-2', { profile: 'work' })
    ])
    listSidebarSessions.mockResolvedValue(
      sidebar({ sessions: [row('work-1', { profile: 'work' })] }, [], [], [
        { profile: 'main', error: 'database locked' }
      ])
    )

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'main' }))

    await act(async () => {
      await result.current.refreshSessions()
    })

    const ids = $sessions.get().map(s => s.id)
    expect(ids).toContain('main-1')
    expect(ids).toContain('main-2')
    expect(ids).toContain('work-1')
    expect(ids).not.toContain('work-2')
  })

  it('preserves everything when the error is unscoped (primary backend down)', async () => {
    setSessions([row('a', { profile: 'default' }), row('b', { profile: 'work' })])
    listSidebarSessions.mockResolvedValue(
      sidebar({ sessions: [] }, [], [], [{ profile: 'primary', error: 'backend unreachable' }])
    )

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'default' }))

    await act(async () => {
      await result.current.refreshSessions()
    })

    expect($sessions.get().map(s => s.id).sort()).toEqual(['a', 'b'])
  })

  it('does not resurrect a tombstoned row of an errored profile', async () => {
    removed.ids = new Set(['main-2'])
    setSessions([row('main-1', { profile: 'main' }), row('main-2', { profile: 'main' })])
    listSidebarSessions.mockResolvedValue(
      sidebar({ sessions: [] }, [], [], [{ profile: 'main', error: 'database locked' }])
    )

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'main' }))

    await act(async () => {
      await result.current.refreshSessions()
    })

    expect($sessions.get().map(s => s.id)).toEqual(['main-1'])
  })
})

describe('refreshSessions initial-fetch failure', () => {
  it('keeps the loading state when the first fetch fails on an empty list', async () => {
    // Cold-start race: the sidebar refresh fired before the (remote) backend
    // was ready. A terminal "no sessions yet" would look exactly like data
    // loss until the user happened to trigger another refresh — keep the
    // skeletons instead, and resolve them on the next successful refresh.
    listSidebarSessions.mockRejectedValue(new Error('backend unreachable'))

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'default' }))

    await act(async () => {
      await expect(result.current.refreshSessions()).rejects.toThrow('backend unreachable')
    })

    expect($sessionsLoading.get()).toBe(true)
    expect($sessions.get()).toEqual([])

    listSidebarSessions.mockResolvedValue(sidebar({ sessions: [row('a')] }))

    await act(async () => {
      await result.current.refreshSessions()
    })

    expect($sessionsLoading.get()).toBe(false)
    expect($sessions.get().map(s => s.id)).toEqual(['a'])
  })

  it('keeps an already-populated list on a failed refresh', async () => {
    setSessions([row('a'), row('b')])
    listSidebarSessions.mockRejectedValue(new Error('backend unreachable'))

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'default' }))

    await act(async () => {
      await expect(result.current.refreshSessions()).rejects.toThrow('backend unreachable')
    })

    expect($sessions.get().map(s => s.id)).toEqual(['a', 'b'])
    expect($sessionsLoading.get()).toBe(false)
  })
})
