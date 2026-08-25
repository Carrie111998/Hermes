// Bulk archive/delete for the sidebar's multi-selection: each row runs the
// same single-row path (optimistic eviction, per-row rollback) as a manual
// archive/delete, but bounded to at most 6 in flight at once so a big
// selection doesn't open one socket per row nor sit serially for minutes.
import { act, cleanup, render, waitFor } from '@testing-library/react'
import type { MutableRefObject } from 'react'
import { useEffect } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { deleteSession, type SessionInfo, setSessionArchived } from '@/hermes'
import { $pinnedSessionIds } from '@/store/layout'
import { notify } from '@/store/notifications'
import { setSessions } from '@/store/session'
import { $selectedSessionIds, $selectionModeActive, enterSelectionMode, toggleSessionSelection } from '@/store/session-selection'

import type { ClientSessionState } from '../../../types'

import { useSessionActions } from './index'

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  deleteSession: vi.fn(),
  getSession: vi.fn(),
  getAllSessionMessages: vi.fn(),
  getLatestSessionMessages: vi.fn(),
  listAllProfileSessions: vi.fn(),
  setApiRequestProfile: vi.fn(),
  setSessionArchived: vi.fn()
}))

vi.mock('@/store/profile', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ensureGatewayProfile: vi.fn().mockResolvedValue(undefined)
}))

vi.mock('@/store/notifications', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  notify: vi.fn(),
  notifyError: vi.fn()
}))

function session(id: string, overrides: Partial<SessionInfo> = {}): SessionInfo {
  return {
    archived: false,
    ended_at: null,
    id,
    input_tokens: 0,
    is_active: false,
    last_active: 1,
    message_count: 0,
    model: null,
    output_tokens: 0,
    preview: null,
    source: 'desktop',
    started_at: 1,
    title: id,
    tool_call_count: 0,
    ...overrides
  } as SessionInfo
}

type Handle = Pick<ReturnType<typeof useSessionActions>, 'archiveSession' | 'archiveSessions' | 'removeSession'>

function Harness({ onReady }: { onReady: (handle: Handle) => void }) {
  const ref = <T,>(value: T): MutableRefObject<T> => ({ current: value })

  const actions = useSessionActions({
    activeSessionId: null,
    activeSessionIdRef: ref<string | null>(null),
    busyRef: ref(false),
    creatingSessionRef: ref(false),
    ensureSessionState: () => ({}) as ClientSessionState,
    getRouteToken: () => 'token',
    getRoutedStoredSessionId: () => null,
    navigate: vi.fn() as never,
    requestGateway: vi.fn().mockResolvedValue(undefined),
    resetViewSync: vi.fn(),
    runtimeIdByStoredSessionIdRef: ref(new Map<string, string>()),
    selectedStoredSessionId: null,
    selectedStoredSessionIdRef: ref<string | null>(null),
    sessionStateByRuntimeIdRef: ref(new Map<string, ClientSessionState>()),
    syncSessionStateToView: vi.fn(),
    updateSessionState: () => ({}) as ClientSessionState
  })

  useEffect(() => {
    onReady({ archiveSession: actions.archiveSession, archiveSessions: actions.archiveSessions, removeSession: actions.removeSession })
  }, [actions, onReady])

  return null
}

async function mountHarness(): Promise<Handle> {
  let handle: Handle | undefined
  render(<Harness onReady={h => (handle = h)} />)
  await waitFor(() => expect(handle).toBeDefined())

  return handle as Handle
}

// A controllable "socket": each call parks itself until the test releases it,
// so the assertions can inspect exactly how many are in flight at once.
function deferredGate() {
  const inFlight = new Set<string>()
  let maxInFlight = 0
  const releasers = new Map<string, () => void>()

  const call = vi.fn((id: string) => {
    inFlight.add(id)
    maxInFlight = Math.max(maxInFlight, inFlight.size)

    return new Promise<{ ok: true }>(resolve => {
      releasers.set(id, () => {
        inFlight.delete(id)
        resolve({ ok: true })
      })
    })
  })

  return {
    call,
    getMaxInFlight: () => maxInFlight,
    inFlight,
    release: (id: string) => releasers.get(id)?.()
  }
}

describe('archiveSessions concurrency + rollback', () => {
  beforeEach(() => {
    setSessions([])
    $pinnedSessionIds.set([])
    vi.mocked(setSessionArchived).mockReset()
    vi.mocked(notify).mockClear()
  })

  afterEach(() => {
    cleanup()
    setSessions([])
    $pinnedSessionIds.set([])
  })

  it('never runs more than 6 archive calls at once for a larger selection', async () => {
    const ids = Array.from({ length: 10 }, (_, i) => `s${i}`)
    setSessions(ids.map(id => session(id)))

    const gate = deferredGate()
    vi.mocked(setSessionArchived).mockImplementation((id: string) => gate.call(id))

    const handle = await mountHarness()
    const bulk = handle.archiveSessions(ids)

    // Let the worker pool's microtasks run and claim their first ids.
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(gate.inFlight.size).toBe(6)

    // Release them in waves; the pool should never exceed the cap even as
    // finished workers pick up the next id.
    while (gate.inFlight.size > 0) {
      const next = [...gate.inFlight]
      await act(async () => {
        for (const id of next) {
          gate.release(id)
        }

        await Promise.resolve()
        await Promise.resolve()
      })

      expect(gate.getMaxInFlight()).toBeLessThanOrEqual(6)
    }

    await bulk
    expect(gate.call).toHaveBeenCalledTimes(10)
  })

  it('rolls back only the failed row and reports one summary error toast', async () => {
    setSessions([session('ok-1'), session('fails'), session('ok-2')])
    $pinnedSessionIds.set(['fails'])

    vi.mocked(setSessionArchived).mockImplementation((id: string) =>
      id === 'fails' ? Promise.reject(new Error('backend down')) : Promise.resolve({ ok: true })
    )

    const handle = await mountHarness()
    await act(() => handle.archiveSessions(['ok-1', 'fails', 'ok-2']))

    // The failed row's session AND its pin come back; the two that succeeded
    // stay gone.
    expect($pinnedSessionIds.get()).toEqual(['fails'])

    // One summary toast for the whole selection, not one per row.
    const errorToasts = vi.mocked(notify).mock.calls.filter(([opts]) => opts.kind === 'error')
    expect(errorToasts).toHaveLength(1)
  })
})

// A row-menu archive/delete while selection mode is active runs OUTSIDE
// `runBulk` (which only clears/prunes for the multi-select bar's own bulk
// verbs), so the single-row path must prune the removed id itself or a
// "0 selected" action bar lingers with a dead id inside it.
describe('single-row archive/delete selection pruning', () => {
  beforeEach(() => {
    setSessions([])
    vi.mocked(setSessionArchived).mockReset()
    vi.mocked(deleteSession).mockReset()
  })

  afterEach(() => {
    cleanup()
    setSessions([])
    $selectedSessionIds.set([])
    $selectionModeActive.set(false)
  })

  it('prunes the archived row from the selection but keeps the rest selected', async () => {
    setSessions([session('ok-1'), session('ok-2')])
    vi.mocked(setSessionArchived).mockResolvedValue({ ok: true } as never)
    enterSelectionMode('ok-1')
    toggleSessionSelection('test', 'ok-2')

    const handle = await mountHarness()
    await act(() => handle.archiveSession('ok-1'))

    expect($selectedSessionIds.get()).toEqual(['ok-2'])
    expect($selectionModeActive.get()).toBe(true)
  })

  it('exits selection mode when the archived row was the only one selected', async () => {
    setSessions([session('solo')])
    vi.mocked(setSessionArchived).mockResolvedValue({ ok: true } as never)
    enterSelectionMode('solo')

    const handle = await mountHarness()
    await act(() => handle.archiveSession('solo'))

    expect($selectedSessionIds.get()).toEqual([])
    expect($selectionModeActive.get()).toBe(false)
  })

  it('leaves the selection untouched when the archive fails and the row is restored', async () => {
    setSessions([session('fails')])
    vi.mocked(setSessionArchived).mockRejectedValue(new Error('backend down'))
    enterSelectionMode('fails')

    const handle = await mountHarness()
    await act(() => handle.archiveSession('fails', { quiet: true }))

    expect($selectedSessionIds.get()).toEqual(['fails'])
    expect($selectionModeActive.get()).toBe(true)
  })

  it('prunes the deleted row from the selection on a successful single-row delete', async () => {
    setSessions([session('ok-1'), session('ok-2')])
    vi.mocked(deleteSession).mockResolvedValue(undefined as never)
    enterSelectionMode('ok-1')
    toggleSessionSelection('test', 'ok-2')

    const handle = await mountHarness()
    await act(() => handle.removeSession('ok-1'))

    expect($selectedSessionIds.get()).toEqual(['ok-2'])
    expect($selectionModeActive.get()).toBe(true)
  })

  it('leaves the selection untouched when the delete fails and the row is restored', async () => {
    setSessions([session('fails')])
    vi.mocked(deleteSession).mockRejectedValue(new Error('backend down'))
    enterSelectionMode('fails')

    const handle = await mountHarness()
    await act(() => handle.removeSession('fails', { quiet: true }))

    expect($selectedSessionIds.get()).toEqual(['fails'])
    expect($selectionModeActive.get()).toBe(true)
  })
})
