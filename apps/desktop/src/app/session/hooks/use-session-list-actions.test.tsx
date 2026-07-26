import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo, SidebarSessionsResponse } from '@/hermes'
import { cronJobVisibilityKey } from '@/lib/cron-session-visibility'
import { $cronJobsHiddenFromSessions } from '@/store/cron'
import { $sessionsLimit, resetSessionsLimit, SIDEBAR_SESSIONS_PAGE_SIZE } from '@/store/layout'
import {
  $cronSessions,
  $cronSessionsInSessionList,
  $cronSessionsTruncated,
  $messagingSessions,
  $sessions,
  $sessionsLoading,
  setCronSessions,
  setCronSessionsAcquisitionTruncated,
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
const getCronJobRuns = vi.fn()

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getCronJobs: vi.fn(async () => []),
  getCronJobRuns: (...args: unknown[]) => getCronJobRuns(...args),
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
  getCronJobRuns.mockReset()
  removed.ids = new Set()
  $cronJobsHiddenFromSessions.set([])
  resetSessionsLimit()
  setSessions([])
  setCronSessions([])
  setCronSessionsAcquisitionTruncated(false)
  setMessagingSessions([])
  setSessionsLoading(false)
})

afterEach(() => {
  setSessions([])
  setCronSessions([])
  setCronSessionsAcquisitionTruncated(false)
  setMessagingSessions([])
  setSessionsLoading(false)
  $cronJobsHiddenFromSessions.set([])
  resetSessionsLimit()
})

describe('refreshSessions identity + loading hygiene', () => {
  it('keeps the previous $sessions array when the refresh is content-identical', async () => {
    const rows = [row('a'), row('b')]
    listSidebarSessions.mockResolvedValue(sidebar({ sessions: rows, total: 2, profile_totals: { default: 2 } }))

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'default' }))

    await act(async () => {
      await result.current.refreshSessions()
    })

    const first = $sessions.get()
    expect(first.map(s => s.id)).toEqual(['a', 'b'])

    // Second refresh returns fresh (but equal) row objects, as the API does.
    listSidebarSessions.mockResolvedValue(
      sidebar({ sessions: [row('a'), row('b')], total: 2, profile_totals: { default: 2 } })
    )

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

  it('drops rows the user just deleted, even when the backend page still lists them', async () => {
    // A delete RPC is in flight: the row is tombstoned optimistically but the
    // batched refresh still carries it (and a lineage-tip variant). Both must be
    // filtered so the optimistic removal never flashes back.
    removed.ids = new Set(['b', 'root-c'])
    listSidebarSessions.mockResolvedValue(
      sidebar({
        sessions: [row('a'), row('b'), row('c', { _lineage_root_id: 'root-c' } as Partial<SessionInfo>)],
        total: 3,
        profile_totals: {}
      })
    )

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'default' }))

    await act(async () => {
      await result.current.refreshSessions()
    })

    expect($sessions.get().map(s => s.id)).toEqual(['a'])
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
  it('makes a single sidebar call and distributes recents / cron / messaging', async () => {
    const recents = [row('a'), row('b')]
    const cron = [row('c1', { source: 'cron', title: 'nightly' })]
    const messaging = [row('m1', { source: 'telegram', title: 'tg chat' })]

    listSidebarSessions.mockResolvedValue(
      sidebar({ sessions: recents, total: 2, profile_totals: { default: 2 } }, cron, messaging)
    )

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
        cronProfile: 'work',
        cronLimit: 50,
        messagingExclude: expect.arrayContaining(['cron'])
      })
    )
  })

  it('filters hidden jobs before the Sessions limit and asks for one bounded cron window', async () => {
    $cronJobsHiddenFromSessions.set([cronJobVisibilityKey('noisy', 'work')])
    $sessionsLimit.set(SIDEBAR_SESSIONS_PAGE_SIZE)

    const noisy = Array.from({ length: 55 }, (_, index) =>
      row(`cron_noisy_${index}`, { profile: 'work', source: 'cron', started_at: 1_000 - index })
    )

    const useful = row('cron_useful_1', { profile: 'work', source: 'cron', started_at: 900 })

    listSidebarSessions.mockResolvedValue(
      sidebar({ sessions: [], total: 0, profile_totals: { work: 0 } }, [...noisy, useful])
    )

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'work' }))

    await act(async () => {
      await result.current.refreshSessions()
    })

    expect(listSidebarSessions).toHaveBeenCalledWith(expect.objectContaining({ cronLimit: 500, cronProfile: 'work' }))
    expect($cronSessionsInSessionList.get().map(session => session.id)).toEqual(['cron_useful_1'])
    expect(getCronJobRuns).not.toHaveBeenCalled()
  })

  it.each([500, 550])('caps a %i-row cron response at 500 and keeps truncation conservative', async rowCount => {
    $cronJobsHiddenFromSessions.set([cronJobVisibilityKey('hidden')])

    const cron = Array.from({ length: rowCount }, (_, index) =>
      row(`cron_visible_${index}`, { source: 'cron', started_at: 1_000 - index })
    )

    listSidebarSessions.mockResolvedValue(sidebar({ sessions: [], total: 0, profile_totals: {} }, cron))

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'default' }))

    await act(async () => {
      await result.current.refreshSessions()
    })

    expect($cronSessions.get()).toHaveLength(500)
    expect($cronSessionsTruncated.get()).toBe(true)
    expect(listSidebarSessions).toHaveBeenCalledWith(expect.objectContaining({ cronLimit: 500 }))
  })

  it('recomputes from cache immediately and performs exactly one batched backfill', async () => {
    setCronSessions([row('cron_daily_1', { source: 'cron' }), row('cron_weekly_1', { source: 'cron' })])
    let resolveRefresh!: (value: SidebarSessionsResponse) => void
    listSidebarSessions.mockReturnValue(
      new Promise<SidebarSessionsResponse>(resolve => {
        resolveRefresh = resolve
      })
    )
    const { result } = renderHook(() => useSessionListActions({ profileScope: 'default' }))

    let pending!: Promise<void>
    act(() => {
      pending = result.current.setCronJobSessionsVisibility('daily', 'default', false)
    })

    expect($cronSessionsInSessionList.get().map(session => session.id)).toEqual(['cron_weekly_1'])
    expect(listSidebarSessions).toHaveBeenCalledTimes(1)
    expect(getCronJobRuns).not.toHaveBeenCalled()

    resolveRefresh(sidebar({ sessions: [], total: 0, profile_totals: {} }, $cronSessions.get()))
    await act(async () => pending)
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
