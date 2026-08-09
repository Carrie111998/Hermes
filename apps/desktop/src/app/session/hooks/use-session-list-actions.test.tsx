import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo, SidebarSessionsResponse } from '@/hermes'
import { $cronJobs, setCronJobs } from '@/store/cron'
import { ALL_PROFILES } from '@/store/profile'
import {
  $cronSessions,
  $messagingPlatformTotals,
  $messagingSessions,
  $messagingTruncated,
  $sessions,
  $sessionsLoading,
  setCronSessions,
  setMessagingPlatformTotals,
  setMessagingSessions,
  setMessagingTruncated,
  setSessions,
  setSessionsLoading
} from '@/store/session'
import type { CronJob } from '@/types/hermes'

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

const job = (id: string): CronJob => ({ enabled: true, id })

// Batched sidebar response builder. `refreshSessions` now makes ONE
// listSidebarSessions call that returns all three slices, replacing the three
// separate listAllProfileSessions calls (each of which reopened every profile
// DB) — #66377-adjacent perf work from the desktop audit canvas.
const sidebar = (
  recents: { sessions: SessionInfo[]; profiles_truncated?: Record<string, boolean> },
  cron: SessionInfo[] = [],
  messaging: SessionInfo[] = []
): SidebarSessionsResponse => ({
  recents: { sessions: recents.sessions, profiles_truncated: recents.profiles_truncated },
  cron: { sessions: cron },
  messaging: { sessions: messaging }
})

const listSidebarSessions = vi.fn()
const listAllProfileSessions = vi.fn()
const getCronJobs = vi.fn()

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void

  const promise = new Promise<T>(done => {
    resolve = done
  })

  return { promise, resolve }
}

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getCronJobs: (...args: unknown[]) => getCronJobs(...args),
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
  getCronJobs.mockReset()
  getCronJobs.mockResolvedValue([])
  removed.ids = new Set()
  setSessions([])
  setCronSessions([])
  setCronJobs([])
  setMessagingSessions([])
  setMessagingPlatformTotals({})
  setMessagingTruncated(false)
  setSessionsLoading(false)
})

afterEach(() => {
  setSessions([])
  setCronSessions([])
  setCronJobs([])
  setMessagingSessions([])
  setMessagingPlatformTotals({})
  setMessagingTruncated(false)
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

describe('profile scope re-home boundary', () => {
  it('clears every profile-scoped secondary store in the scope-change commit', () => {
    const { rerender } = renderHook(
      ({ profileScope }) => useSessionListActions({ profileScope }),
      { initialProps: { profileScope: 'work' } }
    )

    setCronSessions([row('cron-work', { profile: 'work', source: 'cron' })])
    setCronJobs([job('job-work')])
    setMessagingSessions([row('message-work', { profile: 'work', source: 'telegram' })])
    setMessagingTruncated(true)
    setMessagingPlatformTotals({ telegram: 42 })

    rerender({ profileScope: 'personal' })

    expect($cronSessions.get()).toEqual([])
    expect($cronJobs.get()).toEqual([])
    expect($messagingSessions.get()).toEqual([])
    expect($messagingTruncated.get()).toBe(false)
    expect($messagingPlatformTotals.get()).toEqual({})
  })

  it('rejects a stale batched refresh after the scope changes', async () => {
    const workRequest = deferred<SidebarSessionsResponse>()
    listSidebarSessions.mockReturnValueOnce(workRequest.promise)

    const { rerender, result } = renderHook(
      ({ profileScope }) => useSessionListActions({ profileScope }),
      { initialProps: { profileScope: 'work' } }
    )

    let refresh!: Promise<void>

    await act(async () => {
      refresh = result.current.refreshSessions()
      await Promise.resolve()
    })

    rerender({ profileScope: 'personal' })
    setCronSessions([row('cron-personal', { profile: 'personal', source: 'cron' })])
    setMessagingSessions([row('message-personal', { profile: 'personal', source: 'telegram' })])

    await act(async () => {
      workRequest.resolve(
        sidebar(
          { sessions: [row('recent-work', { profile: 'work' })] },
          [row('cron-work', { profile: 'work', source: 'cron' })],
          [row('message-work', { profile: 'work', source: 'telegram' })]
        )
      )
      await refresh
    })

    expect($cronSessions.get().map(session => session.id)).toEqual(['cron-personal'])
    expect($messagingSessions.get().map(session => session.id)).toEqual(['message-personal'])
    expect(getCronJobs).not.toHaveBeenCalled()
  })

  it('does not let an older batch overwrite a newer dedicated messaging refresh', async () => {
    const batchRequest = deferred<SidebarSessionsResponse>()
    const messagingRequest = deferred<{ sessions: SessionInfo[] }>()
    listSidebarSessions.mockReturnValueOnce(batchRequest.promise)
    listAllProfileSessions.mockReturnValueOnce(messagingRequest.promise)

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'work' }))

    let batchRefresh!: Promise<void>
    let messagingRefresh!: Promise<void>

    await act(async () => {
      batchRefresh = result.current.refreshSessions()
      messagingRefresh = result.current.refreshMessagingSessions()
      await Promise.resolve()
    })

    await act(async () => {
      messagingRequest.resolve({ sessions: [row('message-newer', { profile: 'work', source: 'telegram' })] })
      await messagingRefresh
    })

    await act(async () => {
      batchRequest.resolve(
        sidebar(
          { sessions: [row('recent-work', { profile: 'work' })] },
          [],
          [row('message-older', { profile: 'work', source: 'telegram' })]
        )
      )
      await batchRefresh
    })

    expect($messagingSessions.get().map(session => session.id)).toEqual(['message-newer'])
  })

  it('rejects stale and out-of-order cron jobs refreshes', async () => {
    const staleWorkRequest = deferred<CronJob[]>()
    const olderPersonalRequest = deferred<CronJob[]>()
    const newerPersonalRequest = deferred<CronJob[]>()
    getCronJobs
      .mockReturnValueOnce(staleWorkRequest.promise)
      .mockReturnValueOnce(olderPersonalRequest.promise)
      .mockReturnValueOnce(newerPersonalRequest.promise)

    const { rerender, result } = renderHook(
      ({ profileScope }) => useSessionListActions({ profileScope }),
      { initialProps: { profileScope: 'work' } }
    )

    let staleWorkRefresh!: Promise<void>
    let olderPersonalRefresh!: Promise<void>
    let newerPersonalRefresh!: Promise<void>

    await act(async () => {
      staleWorkRefresh = result.current.refreshCronJobs()
      await Promise.resolve()
    })

    rerender({ profileScope: 'personal' })

    await act(async () => {
      olderPersonalRefresh = result.current.refreshCronJobs()
      newerPersonalRefresh = result.current.refreshCronJobs()
      await Promise.resolve()
    })

    await act(async () => {
      newerPersonalRequest.resolve([job('job-personal-newer')])
      await newerPersonalRefresh
      olderPersonalRequest.resolve([job('job-personal-older')])
      await olderPersonalRefresh
      staleWorkRequest.resolve([job('job-work')])
      await staleWorkRefresh
    })

    expect($cronJobs.get().map(job => job.id)).toEqual(['job-personal-newer'])
  })

  it('rejects an old A batch after an A to B to A transition', async () => {
    const oldWorkRequest = deferred<SidebarSessionsResponse>()
    listSidebarSessions.mockReturnValueOnce(oldWorkRequest.promise)

    const { rerender, result } = renderHook(
      ({ profileScope }) => useSessionListActions({ profileScope }),
      { initialProps: { profileScope: 'work' } }
    )

    let oldWorkRefresh!: Promise<void>

    await act(async () => {
      oldWorkRefresh = result.current.refreshSessions()
      await Promise.resolve()
    })

    rerender({ profileScope: 'personal' })
    rerender({ profileScope: 'work' })
    setCronSessions([row('cron-current-work', { profile: 'work', source: 'cron' })])
    setMessagingSessions([row('message-current-work', { profile: 'work', source: 'telegram' })])

    await act(async () => {
      oldWorkRequest.resolve(
        sidebar(
          { sessions: [row('recent-old-work', { profile: 'work' })] },
          [row('cron-old-work', { profile: 'work', source: 'cron' })],
          [row('message-old-work', { profile: 'work', source: 'telegram' })]
        )
      )
      await oldWorkRefresh
    })

    expect($cronSessions.get().map(session => session.id)).toEqual(['cron-current-work'])
    expect($messagingSessions.get().map(session => session.id)).toEqual(['message-current-work'])
    expect(getCronJobs).not.toHaveBeenCalled()
  })

  it('preserves ALL_PROFILES as a real scope while re-homing its secondary stores', async () => {
    listSidebarSessions.mockResolvedValue(sidebar({ sessions: [] }))

    const { rerender, result } = renderHook(
      ({ profileScope }) => useSessionListActions({ profileScope }),
      { initialProps: { profileScope: 'work' } }
    )

    setCronSessions([row('cron-work', { profile: 'work', source: 'cron' })])
    rerender({ profileScope: ALL_PROFILES })

    expect($cronSessions.get()).toEqual([])

    await act(async () => {
      await result.current.refreshSessions()
      await result.current.refreshCronJobs()
    })

    expect(listSidebarSessions).toHaveBeenLastCalledWith(expect.objectContaining({ recentsProfile: 'all' }))
    expect(getCronJobs).toHaveBeenLastCalledWith('all')
  })
})

describe('messaging session profile scope', () => {
  it('keeps the newest messaging refresh when requests resolve out of order', async () => {
    const olderRequest = deferred<{ sessions: SessionInfo[] }>()
    const newerRequest = deferred<{ sessions: SessionInfo[] }>()
    listAllProfileSessions.mockReturnValueOnce(olderRequest.promise).mockReturnValueOnce(newerRequest.promise)

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'work' }))

    let olderRefresh!: Promise<void>
    let newerRefresh!: Promise<void>

    await act(async () => {
      olderRefresh = result.current.refreshMessagingSessions()
      newerRefresh = result.current.refreshMessagingSessions()
      await Promise.resolve()
    })

    await act(async () => {
      newerRequest.resolve({ sessions: [row('newer', { profile: 'work', source: 'telegram' })] })
      await newerRefresh
    })

    await act(async () => {
      olderRequest.resolve({ sessions: [row('older', { profile: 'work', source: 'telegram' })] })
      await olderRefresh
    })

    expect($messagingSessions.get().map(session => session.id)).toEqual(['newer'])
  })

  it('keeps the newest same-platform page when requests resolve out of order', async () => {
    const olderRequest = deferred<{ sessions: SessionInfo[]; total: number }>()
    const newerRequest = deferred<{ sessions: SessionInfo[]; total: number }>()
    listAllProfileSessions.mockReturnValueOnce(olderRequest.promise).mockReturnValueOnce(newerRequest.promise)

    const { result } = renderHook(() => useSessionListActions({ profileScope: 'work' }))

    let olderLoadMore!: Promise<void>
    let newerLoadMore!: Promise<void>

    await act(async () => {
      olderLoadMore = result.current.loadMoreMessagingForPlatform('telegram')
      newerLoadMore = result.current.loadMoreMessagingForPlatform('telegram')
      await Promise.resolve()
    })

    await act(async () => {
      newerRequest.resolve({
        sessions: [row('newer', { profile: 'work', source: 'telegram' })],
        total: 1
      })
      await newerLoadMore
    })

    await act(async () => {
      olderRequest.resolve({
        sessions: [row('older', { profile: 'work', source: 'telegram' })],
        total: 1
      })
      await olderLoadMore
    })

    expect($messagingSessions.get().map(session => session.id)).toEqual(['newer'])
    expect($messagingPlatformTotals.get()).toEqual({ telegram: 1 })
  })

  it('does not publish a refresh that resolves after the profile scope changes', async () => {
    const workRequest = deferred<{ sessions: SessionInfo[] }>()
    listAllProfileSessions.mockReturnValueOnce(workRequest.promise)

    const { rerender, result } = renderHook(
      ({ profileScope }) => useSessionListActions({ profileScope }),
      { initialProps: { profileScope: 'work' } }
    )

    let refresh!: Promise<void>

    await act(async () => {
      refresh = result.current.refreshMessagingSessions()
      await Promise.resolve()
    })

    rerender({ profileScope: 'personal' })
    setMessagingSessions([row('personal-row', { profile: 'personal', source: 'telegram' })])

    await act(async () => {
      workRequest.resolve({ sessions: [row('work-row', { profile: 'work', source: 'telegram' })] })
      await refresh
    })

    expect($messagingSessions.get().map(session => session.id)).toEqual(['personal-row'])
  })

  it('does not publish a platform page that resolves after the profile scope changes', async () => {
    setMessagingSessions([row('work-seed', { profile: 'work', source: 'telegram' })])
    const workRequest = deferred<{ sessions: SessionInfo[]; total: number }>()
    listAllProfileSessions.mockReturnValueOnce(workRequest.promise)

    const { rerender, result } = renderHook(
      ({ profileScope }) => useSessionListActions({ profileScope }),
      { initialProps: { profileScope: 'work' } }
    )

    let loadMore!: Promise<void>

    await act(async () => {
      loadMore = result.current.loadMoreMessagingForPlatform('telegram')
      await Promise.resolve()
    })

    rerender({ profileScope: 'personal' })
    setMessagingSessions([row('personal-row', { profile: 'personal', source: 'telegram' })])

    await act(async () => {
      workRequest.resolve({
        sessions: [row('work-row', { profile: 'work', source: 'telegram' })],
        total: 2
      })
      await loadMore
    })

    expect($messagingSessions.get().map(session => session.id)).toEqual(['personal-row'])
    expect($messagingPlatformTotals.get()).toEqual({})
  })

  it('resets exact platform totals when the profile scope changes, including ALL_PROFILES', () => {
    const { rerender } = renderHook(
      ({ profileScope }) => useSessionListActions({ profileScope }),
      { initialProps: { profileScope: 'work' } }
    )

    setMessagingPlatformTotals({ telegram: 42 })

    rerender({ profileScope: ALL_PROFILES })

    expect($messagingPlatformTotals.get()).toEqual({})
  })

  it('refreshes messaging sessions for the concrete profile and maps ALL_PROFILES to all', async () => {
    listAllProfileSessions.mockResolvedValue({ sessions: [] })

    const { rerender, result } = renderHook(
      ({ profileScope }) => useSessionListActions({ profileScope }),
      { initialProps: { profileScope: 'work' } }
    )

    await act(async () => {
      await result.current.refreshMessagingSessions()
    })

    expect(listAllProfileSessions).toHaveBeenLastCalledWith(
      expect.any(Number),
      1,
      'exclude',
      'recent',
      'work',
      expect.any(Object)
    )

    rerender({ profileScope: ALL_PROFILES })

    await act(async () => {
      await result.current.refreshMessagingSessions()
    })

    expect(listAllProfileSessions).toHaveBeenLastCalledWith(
      expect.any(Number),
      1,
      'exclude',
      'recent',
      'all',
      expect.any(Object)
    )
  })

  it('loads more messaging sessions for the concrete profile and maps ALL_PROFILES to all', async () => {
    listAllProfileSessions.mockResolvedValue({ sessions: [], total: 0 })

    const { rerender, result } = renderHook(
      ({ profileScope }) => useSessionListActions({ profileScope }),
      { initialProps: { profileScope: 'work' } }
    )

    await act(async () => {
      await result.current.loadMoreMessagingForPlatform('telegram')
    })

    expect(listAllProfileSessions).toHaveBeenLastCalledWith(
      expect.any(Number),
      1,
      'exclude',
      'recent',
      'work',
      expect.objectContaining({ source: 'telegram' })
    )

    rerender({ profileScope: ALL_PROFILES })

    await act(async () => {
      await result.current.loadMoreMessagingForPlatform('telegram')
    })

    expect(listAllProfileSessions).toHaveBeenLastCalledWith(
      expect.any(Number),
      1,
      'exclude',
      'recent',
      'all',
      expect.objectContaining({ source: 'telegram' })
    )
  })
})
