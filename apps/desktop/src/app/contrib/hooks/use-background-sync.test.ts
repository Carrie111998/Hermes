import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { PRE_TURN_LIVE_SETTLE_GRACE_MS } from '@/app/session/hooks/use-message-stream/utils'
import { createClientSessionState } from '@/lib/chat-runtime'
import { sessionMessagesSignature } from '@/lib/session-signatures'
import { $changeEventsAvailable, notifySessionsChanged, resetLiveSync } from '@/store/live-sync'
import {
  $activeSessionId,
  $selectedStoredSessionId,
  setBusy,
  setMessagingSessions,
  setSessionOwnerHint,
  setSessions
} from '@/store/session'
import {
  $attentionSessionIds,
  $sessionStates,
  $sessionTiles,
  $stalledSessionIds,
  $workingSessionIds,
  clearAllSessionStates,
  publishSessionState,
  SESSION_WATCHDOG_TIMEOUT_MS
} from '@/store/session-states'

import {
  type ActiveTranscriptRefreshDeps,
  isTypingBurstActive,
  noteRendererKeyboardActivity,
  reconcileActiveTranscript,
  reconcileTileTranscripts as reconcileTileTranscriptsForTest,
  rehydrateLiveSessionStatuses,
  resetLiveRuntimeTracking,
  resetTypingActivityTracking,
  resolveActiveTranscriptSession,
  useBackgroundSync,
  windowIsActivelyViewed
} from './use-background-sync'

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal()),
  getLatestSessionMessages: vi.fn()
}))

const { getLatestSessionMessages } = await import('@/hermes')

const ACTIVE_RUNTIME_ID = 'runtime-active'
const ACTIVE_STORED_ID = 'stored-active'

function transcript(answer: string, sessionId = ACTIVE_STORED_ID) {
  return {
    messages: [
      { content: 'question', role: 'user', timestamp: 1 },
      { content: answer, role: 'assistant', timestamp: 2 }
    ],
    session_id: sessionId
  }
}

function makeRefresh(resolveSession: ActiveTranscriptRefreshDeps['resolveSession'] = () => ({ profile: 'default' })) {
  const activeSessionIdRef = { current: ACTIVE_RUNTIME_ID as string | null }
  const selectedStoredSessionIdRef = { current: ACTIVE_STORED_ID as string | null }
  const busyRef = { current: false }
  const requestSequenceRef = { current: 0 }
  const signatureRef = { current: new Map<string, string>() }
  const state = createClientSessionState(ACTIVE_STORED_ID)
  const states = new Map([[ACTIVE_RUNTIME_ID, state]])

  const updateSessionStateRef = {
    updateSessionState: vi.fn((sessionId: string, updater: (value: typeof state) => typeof state) => {
      const next = updater(states.get(sessionId) ?? createClientSessionState(ACTIVE_STORED_ID))
      states.set(sessionId, next)

      return next
    })
  }

  const { updateSessionState } = updateSessionStateRef

  const refresh = () =>
    reconcileActiveTranscript({
      activeSessionIdRef,
      busyRef,
      requestSequenceRef,
      resolveSession,
      selectedStoredSessionIdRef,
      signatureRef,
      updateSessionState
    })

  return { activeSessionIdRef, busyRef, refresh, selectedStoredSessionIdRef, state, states, updateSessionState }
}

function useSyncHarness({
  activeIsMessaging = false,
  activeSessionId,
  activeStoredSessionId,
  refreshActiveTranscript
}: {
  activeIsMessaging?: boolean
  activeSessionId: string | null
  activeStoredSessionId: string | null
  refreshActiveTranscript: () => Promise<void>
}) {
  const updateSessionState: Parameters<typeof useBackgroundSync>[0]['updateSessionState'] = vi.fn(
    (sessionId, updater) => {
      const current = {} as Parameters<typeof updater>[0]

      return updater(current)
    }
  )

  useBackgroundSync({
    activeConnectionId: 'local',
    activeGatewayProfile: 'default',
    activeIsMessaging,
    activeSessionId,
    activeStoredSessionId,
    freshDraftReady: false,
    gatewayState: 'open',
    refreshActiveTranscript,
    refreshCronJobs: vi.fn(),
    refreshCurrentModel: vi.fn(),
    refreshHermesConfig: vi.fn(),
    refreshMessagingSessions: vi.fn(),
    refreshSessions: vi.fn(),
    updateSessionState,
    requestGateway: vi.fn(async () => ({ sessions: [] })) as never
  })
}

function renderSync(
  refreshActiveTranscript: () => Promise<void>,
  options: { activeIsMessaging?: boolean; activeSessionId?: null | string; activeStoredSessionId?: null | string } = {}
) {
  return renderHook(() =>
    useSyncHarness({
      activeSessionId: ACTIVE_RUNTIME_ID,
      activeStoredSessionId: ACTIVE_STORED_ID,
      refreshActiveTranscript,
      ...options
    })
  )
}

beforeEach(() => {
  // visiblePoll only ticks while the window is actively viewed; jsdom's
  // document.hasFocus() is not reliably true, so pin it for these tests.
  vi.spyOn(document, 'hasFocus').mockReturnValue(true)
})

afterEach(() => {
  cleanup()
  vi.clearAllTimers()
  vi.useRealTimers()
  resetLiveSync()
  $activeSessionId.set(null)
  $selectedStoredSessionId.set(null)
  setSessions([])
  setMessagingSessions([])
  setBusy(false)
  vi.clearAllMocks()
  vi.restoreAllMocks()
  $sessionTiles.set([])
  clearAllSessionStates()
  resetLiveRuntimeTracking()
  resetTypingActivityTracking()
})

describe('active transcript refresh', () => {
  beforeEach(() => {
    vi.mocked(getLatestSessionMessages).mockResolvedValue(transcript('answer') as never)
  })

  it('refreshes a hidden session through its unique complete owner route', async () => {
    const hiddenStoredSessionId = 'hidden-bot-chat'

    const ownerRoute = {
      connectionId: 'ssh-bot-owner',
      mode: 'remote' as const,
      profile: 'bot-route',
      targetProfile: 'bot-profile'
    }

    $changeEventsAvailable.set(true)
    $activeSessionId.set(ACTIVE_RUNTIME_ID)
    $selectedStoredSessionId.set(hiddenStoredSessionId)
    setSessionOwnerHint(hiddenStoredSessionId, ownerRoute)
    const fixture = makeRefresh(resolveActiveTranscriptSession)
    fixture.selectedStoredSessionIdRef.current = hiddenStoredSessionId
    vi.mocked(getLatestSessionMessages).mockResolvedValue(
      transcript('hidden external answer', hiddenStoredSessionId) as never
    )

    renderSync(fixture.refresh, { activeStoredSessionId: hiddenStoredSessionId })

    act(() => notifySessionsChanged())

    await waitFor(() =>
      expect(getLatestSessionMessages).toHaveBeenCalledWith(hiddenStoredSessionId, {
        connectionId: ownerRoute.connectionId,
        profile: ownerRoute.targetProfile
      })
    )
    expect(fixture.states.get(ACTIVE_RUNTIME_ID)?.messages.at(-1)?.parts[0]).toMatchObject({
      text: 'hidden external answer'
    })
  })

  it('waits for a bound tile runtime state before persisted reconciliation', async () => {
    const updateSessionState = vi.fn()

    await reconcileTileTranscriptsForTest({
      tiles: [{ storedSessionId: 'stored-without-state', runtimeId: 'runtime-without-state' }],
      requestSequenceRef: { current: 0 },
      signatureRef: { current: new Map<string, string>() },
      updateSessionState
    })

    expect(getLatestSessionMessages).not.toHaveBeenCalled()
    expect(updateSessionState).not.toHaveBeenCalled()
  })

  it('reconciles a workspace TILE transcript when sessions.changed ticks (#94255 review: behavior, not source-grep)', async () => {
    $changeEventsAvailable.set(true)
    // The tile's runtime differs from the active session — it is NOT the main
    // pane surface, so only the tile reconcile path may update it.
    const TILE_RUNTIME_ID = 'runtime-tile'
    const TILE_STORED_ID = 'stored-tile'
    $activeSessionId.set('runtime-something-else')
    $selectedStoredSessionId.set('stored-other')

    publishSessionState(TILE_RUNTIME_ID, createClientSessionState(TILE_STORED_ID))

    let updaterCallCount = 0

    const updateSessionState: Parameters<typeof reconcileTileTranscriptsForTest>[0]['updateSessionState'] = vi.fn(
      (sessionId, updater) => {
        updaterCallCount += 1
        const current = {} as Parameters<typeof updater>[0]

        return updater(current)
      }
    )

    const signatureRef = { current: new Map<string, string>() }
    const requestSequenceRef = { current: 0 }

    vi.mocked(getLatestSessionMessages).mockImplementation(async (storedId: string) => {
      if (storedId === TILE_STORED_ID) {
        return {
          messages: [
            { content: 'tile question', role: 'user', timestamp: 1 },
            { content: 'background delivery answer', role: 'assistant', timestamp: 2 }
          ],
          session_id: TILE_STORED_ID
        } as never
      }

      return transcript('main-pane answer') as never
    })

    // Seed a tile so reconcileTileTranscripts has a target.
    setSessions([]) // bot chats are hidden from $sessions — the whole point

    await act(async () => {
      await reconcileTileTranscriptsForTest({
        tiles: [{ storedSessionId: TILE_STORED_ID, runtimeId: TILE_RUNTIME_ID }],
        requestSequenceRef,
        signatureRef,
        updateSessionState
      })
    })

    // Behavior assertions:
    expect(updaterCallCount).toBeGreaterThan(0)
    expect(getLatestSessionMessages).toHaveBeenCalledWith(TILE_STORED_ID)
  })

  it('routes duplicate stored-id tiles through their exact owners and reconciles both', async () => {
    const sharedStoredId = 'shared-stored-tile'

    const ownerA = {
      connectionId: 'source-a',
      mode: 'remote' as const,
      profile: 'worker',
      targetProfile: 'worker-a'
    }

    const ownerB = {
      connectionId: 'source-b',
      mode: 'remote' as const,
      profile: 'worker',
      targetProfile: 'worker-b'
    }

    $sessionTiles.set([
      { ownerRoute: ownerA, runtimeId: 'runtime-owner-a', storedSessionId: sharedStoredId },
      { ownerRoute: ownerB, runtimeId: 'runtime-owner-b', storedSessionId: sharedStoredId }
    ])
    publishSessionState('runtime-owner-a', createClientSessionState(sharedStoredId))
    publishSessionState('runtime-owner-b', createClientSessionState(sharedStoredId))
    vi.mocked(getLatestSessionMessages).mockResolvedValue(transcript('same persisted answer', sharedStoredId) as never)

    const updateSessionState = vi.fn(
      (
        runtimeId: string,
        updater: (state: ReturnType<typeof createClientSessionState>) => ReturnType<typeof createClientSessionState>
      ) => updater($sessionStates.get()[runtimeId] ?? createClientSessionState(sharedStoredId))
    )

    await reconcileTileTranscriptsForTest({
      requestSequenceRef: { current: 0 },
      signatureRef: { current: new Map<string, string>() },
      updateSessionState
    })

    expect(getLatestSessionMessages).toHaveBeenNthCalledWith(1, sharedStoredId, {
      connectionId: ownerA.connectionId,
      profile: ownerA.targetProfile
    })
    expect(getLatestSessionMessages).toHaveBeenNthCalledWith(2, sharedStoredId, {
      connectionId: ownerB.connectionId,
      profile: ownerB.targetProfile
    })
    expect(updateSessionState.mock.calls.map(([runtimeId]) => runtimeId)).toEqual([
      'runtime-owner-a',
      'runtime-owner-b'
    ])
  })

  it('lets the newest reconciliation pass finish every tile without an older pass invalidating it', async () => {
    const storedA = 'stored-overlap-a'
    const storedB = 'stored-overlap-b'
    const runtimeA = 'runtime-overlap-a'
    const runtimeB = 'runtime-overlap-b'
    let resolveOldA!: (value: ReturnType<typeof transcript>) => void
    let resolveNewB!: (value: ReturnType<typeof transcript>) => void

    const oldA = new Promise<ReturnType<typeof transcript>>(resolve => {
      resolveOldA = resolve
    })

    const newB = new Promise<ReturnType<typeof transcript>>(resolve => {
      resolveNewB = resolve
    })

    let readCount = 0

    $sessionTiles.set([
      { runtimeId: runtimeA, storedSessionId: storedA },
      { runtimeId: runtimeB, storedSessionId: storedB }
    ])
    publishSessionState(runtimeA, createClientSessionState(storedA))
    publishSessionState(runtimeB, createClientSessionState(storedB))
    vi.mocked(getLatestSessionMessages).mockImplementation(async storedId => {
      readCount += 1

      if (readCount === 1) {
        return oldA as never
      }

      if (readCount === 2) {
        return transcript('new A', storedA) as never
      }

      if (readCount === 3) {
        return newB as never
      }

      return transcript('old B', storedId) as never
    })

    const states = new Map<string, ReturnType<typeof createClientSessionState>>()

    const updateSessionState = vi.fn(
      (
        runtimeId: string,
        updater: (state: ReturnType<typeof createClientSessionState>) => ReturnType<typeof createClientSessionState>
      ) => {
        const next = updater($sessionStates.get()[runtimeId] ?? createClientSessionState(null))
        states.set(runtimeId, next)

        return next
      }
    )

    const requestSequenceRef = { current: 0 }
    const signatureRef = { current: new Map<string, string>() }
    const args = { requestSequenceRef, signatureRef, updateSessionState }

    const olderPass = reconcileTileTranscriptsForTest(args)
    await waitFor(() => expect(getLatestSessionMessages).toHaveBeenCalledTimes(1))

    const newerPass = reconcileTileTranscriptsForTest(args)
    await waitFor(() => expect(getLatestSessionMessages).toHaveBeenCalledTimes(3))

    resolveOldA(transcript('old A', storedA))
    await Promise.resolve()
    resolveNewB(transcript('new B', storedB))
    await Promise.all([olderPass, newerPass])

    expect(getLatestSessionMessages).toHaveBeenCalledTimes(3)
    expect(states.get(runtimeA)?.messages.at(-1)?.parts[0]).toMatchObject({ text: 'new A' })
    expect(states.get(runtimeB)?.messages.at(-1)?.parts[0]).toMatchObject({ text: 'new B' })
    expect([...signatureRef.current.values()].sort()).toEqual(
      [
        sessionMessagesSignature(transcript('new A', storedA).messages as never),
        sessionMessagesSignature(transcript('new B', storedB).messages as never)
      ].sort()
    )

    vi.mocked(getLatestSessionMessages).mockImplementation(
      async storedId => transcript(storedId === storedA ? 'new A' : 'new B', storedId) as never
    )
    updateSessionState.mockClear()
    await reconcileTileTranscriptsForTest(args)

    expect(updateSessionState).not.toHaveBeenCalled()
  })

  it('keeps a busy Bot tile untouched and reconciles its idle sibling through the real sessions.changed hook path', async () => {
    const tileRuntimeId = 'runtime-hook-busy-bot-tile'
    const tileStoredId = 'stored-hook-busy-bot-tile'
    const idleRuntimeId = 'runtime-hook-idle-sibling'
    const idleStoredId = 'stored-hook-idle-sibling'
    const refreshSessions = vi.fn(async () => undefined)
    const refreshMessagingSessions = vi.fn(async () => undefined)

    const updateSessionState = vi.fn(
      (
        runtimeId: string,
        updater: (state: ReturnType<typeof createClientSessionState>) => ReturnType<typeof createClientSessionState>
      ) => {
        const next = updater(
          $sessionStates.get()[runtimeId] ??
            createClientSessionState(runtimeId === idleRuntimeId ? idleStoredId : tileStoredId)
        )

        publishSessionState(runtimeId, next)

        return next
      }
    )

    const requestGateway = vi.fn(async () => ({ sessions: [] })) as never
    const refreshActiveTranscript = vi.fn(async () => undefined)
    const refreshCronJobs = vi.fn(async () => undefined)
    const refreshCurrentModel = vi.fn(async () => undefined)
    const refreshHermesConfig = vi.fn(async () => undefined)

    $changeEventsAvailable.set(true)
    $sessionTiles.set([
      { runtimeId: tileRuntimeId, storedSessionId: tileStoredId },
      { runtimeId: idleRuntimeId, storedSessionId: idleStoredId }
    ])
    publishSessionState(tileRuntimeId, {
      ...createClientSessionState(tileStoredId),
      awaitingResponse: true,
      busy: true,
      messages: [
        { id: 'user-live', parts: [{ text: 'question', type: 'text' }], role: 'user' },
        {
          id: 'assistant-stream-live',
          parts: [{ text: 'streaming answer', type: 'text' }],
          pending: true,
          role: 'assistant'
        }
      ]
    })
    publishSessionState(idleRuntimeId, createClientSessionState(idleStoredId))
    vi.mocked(getLatestSessionMessages).mockImplementation(async storedId => {
      if (storedId === tileStoredId) {
        return {
          messages: [{ content: 'question', role: 'user', timestamp: 1 }],
          session_id: tileStoredId
        } as never
      }

      return transcript('idle sibling update', idleStoredId) as never
    })

    renderHook(() =>
      useBackgroundSync({
        activeConnectionId: 'local',
        activeGatewayProfile: 'default',
        activeIsMessaging: false,
        activeSessionId: null,
        activeStoredSessionId: null,
        freshDraftReady: false,
        gatewayState: 'open',
        refreshActiveTranscript,
        refreshCronJobs,
        refreshCurrentModel,
        refreshHermesConfig,
        refreshMessagingSessions,
        refreshSessions,
        requestGateway,
        updateSessionState
      })
    )

    await act(async () => Promise.resolve())
    refreshSessions.mockClear()
    refreshMessagingSessions.mockClear()
    vi.mocked(getLatestSessionMessages).mockClear()

    act(() => notifySessionsChanged())
    await waitFor(() => expect(getLatestSessionMessages).toHaveBeenCalledTimes(2))

    expect(refreshSessions).toHaveBeenCalledTimes(1)
    expect(refreshMessagingSessions).toHaveBeenCalledTimes(1)
    expect(getLatestSessionMessages).toHaveBeenCalledWith(tileStoredId)
    expect(getLatestSessionMessages).toHaveBeenCalledWith(idleStoredId)
    expect(updateSessionState).toHaveBeenCalledTimes(2)
    expect(updateSessionState).toHaveBeenCalledWith(tileRuntimeId, expect.any(Function), tileStoredId)
    expect(updateSessionState).toHaveBeenCalledWith(idleRuntimeId, expect.any(Function), idleStoredId)
    expect($sessionStates.get()[tileRuntimeId]).toMatchObject({ awaitingResponse: true, busy: true })
    expect($sessionStates.get()[tileRuntimeId]?.messages.at(-1)?.parts[0]).toMatchObject({
      text: 'streaming answer'
    })
  })

  it('reconciles a tile after an idle active-list row releases stale live ownership', async () => {
    const tileRuntimeId = 'runtime-hook-recovered-bot-tile'
    const tileStoredId = 'stored-hook-recovered-bot-tile'

    let activeListCalls = 0

    let resolveRecoveredList!: (value: { sessions: Array<{ id: string; session_key: string; status: 'idle' }> }) => void

    const recoveredList = new Promise<{
      sessions: Array<{ id: string; session_key: string; status: 'idle' }>
    }>(resolve => {
      resolveRecoveredList = resolve
    })

    const requestGatewayMock = vi.fn(async (method: string) => {
      if (method !== 'session.active_list') {
        return {}
      }

      activeListCalls += 1

      if (activeListCalls === 1) {
        return {
          sessions: [{ id: tileRuntimeId, session_key: tileStoredId, status: 'working' }]
        }
      }

      return recoveredList
    })

    const requestGateway = requestGatewayMock as never

    let canonicalState: ReturnType<typeof createClientSessionState> = {
      ...createClientSessionState(tileStoredId),
      awaitingResponse: true,
      busy: true,
      messages: [
        { id: 'user-canonical', parts: [{ text: 'question', type: 'text' as const }], role: 'user' as const },
        {
          id: 'assistant-stream-canonical',
          parts: [{ text: 'recovered final', type: 'text' as const }],
          pending: true,
          role: 'assistant' as const
        }
      ],
      turnLive: true
    }

    const updateSessionState = vi.fn(
      (
        _runtimeId: string,
        updater: (state: ReturnType<typeof createClientSessionState>) => ReturnType<typeof createClientSessionState>
      ) => {
        canonicalState = updater(canonicalState)
        publishSessionState(tileRuntimeId, canonicalState)

        return canonicalState
      }
    )

    const stable = {
      refreshActiveTranscript: vi.fn(async () => undefined),
      refreshCronJobs: vi.fn(async () => undefined),
      refreshCurrentModel: vi.fn(async () => undefined),
      refreshHermesConfig: vi.fn(async () => undefined),
      refreshMessagingSessions: vi.fn(async () => undefined),
      refreshSessions: vi.fn(async () => undefined)
    }

    $changeEventsAvailable.set(true)
    $sessionTiles.set([{ runtimeId: tileRuntimeId, storedSessionId: tileStoredId }])
    publishSessionState(tileRuntimeId, { ...canonicalState, messages: [] })
    vi.mocked(getLatestSessionMessages).mockResolvedValue(transcript('recovered final answer', tileStoredId) as never)

    renderHook(() =>
      useBackgroundSync({
        activeConnectionId: 'local',
        activeGatewayProfile: 'default',
        activeIsMessaging: false,
        activeSessionId: null,
        activeStoredSessionId: null,
        freshDraftReady: false,
        gatewayState: 'open',
        requestGateway,
        updateSessionState,
        ...stable
      })
    )

    await waitFor(() =>
      expect(requestGatewayMock.mock.calls.filter(([method]) => method === 'session.active_list')).toHaveLength(1)
    )

    act(() => notifySessionsChanged())
    await waitFor(() =>
      expect(requestGatewayMock.mock.calls.filter(([method]) => method === 'session.active_list')).toHaveLength(2)
    )
    expect(getLatestSessionMessages).toHaveBeenCalledOnce()

    resolveRecoveredList({ sessions: [{ id: tileRuntimeId, session_key: tileStoredId, status: 'idle' }] })
    await act(async () => recoveredList)

    // The first idle response is stale because the safe transcript merge changed
    // the runtime state after the status request began. The next current poll
    // performs the authoritative settle and replays the persisted snapshot.
    expect(canonicalState).toMatchObject({ awaitingResponse: true, busy: true, turnLive: true })

    act(() => notifySessionsChanged())
    await waitFor(() =>
      expect(requestGatewayMock.mock.calls.filter(([method]) => method === 'session.active_list')).toHaveLength(3)
    )
    await waitFor(() => expect(canonicalState).toMatchObject({ awaitingResponse: false, busy: false, turnLive: false }))
    await waitFor(() =>
      expect(canonicalState.messages.at(-1)?.parts[0]).toMatchObject({ text: 'recovered final answer' })
    )
    expect(vi.mocked(getLatestSessionMessages).mock.calls.length).toBeGreaterThanOrEqual(2)
    expect(updateSessionState).toHaveBeenCalledWith(tileRuntimeId, expect.any(Function), tileStoredId)
  })

  it('ignores an active-list response from a previous connection with the same profile', async () => {
    const staleRuntimeId = 'runtime-from-stale-connection'
    const staleStoredId = 'stored-from-stale-connection'

    let resolveStaleList!: (value: { sessions: Array<{ id: string; session_key: string; status: 'working' }> }) => void

    const staleList = new Promise<{
      sessions: Array<{ id: string; session_key: string; status: 'working' }>
    }>(resolve => {
      resolveStaleList = resolve
    })

    let activeListCalls = 0

    const requestGatewayMock = vi.fn(async (method: string) => {
      if (method !== 'session.active_list') {
        return {}
      }

      activeListCalls += 1

      return activeListCalls === 1 ? staleList : { sessions: [] }
    })

    const requestGateway = requestGatewayMock as never

    const stable = {
      refreshActiveTranscript: vi.fn(async () => undefined),
      refreshCronJobs: vi.fn(async () => undefined),
      refreshCurrentModel: vi.fn(async () => undefined),
      refreshHermesConfig: vi.fn(async () => undefined),
      refreshMessagingSessions: vi.fn(async () => undefined),
      refreshSessions: vi.fn(async () => undefined),
      updateSessionState: vi.fn(
        (
          _runtimeId: string,
          updater: (state: ReturnType<typeof createClientSessionState>) => ReturnType<typeof createClientSessionState>
        ) => updater(createClientSessionState(staleStoredId))
      )
    }

    const hook = renderHook(
      ({ connectionId }: { connectionId: string }) =>
        useBackgroundSync({
          activeConnectionId: connectionId,
          activeGatewayProfile: 'default',
          activeIsMessaging: false,
          activeSessionId: null,
          activeStoredSessionId: null,
          freshDraftReady: false,
          gatewayState: 'open',
          requestGateway,
          ...stable
        }),
      { initialProps: { connectionId: 'source-a' } }
    )

    await waitFor(() =>
      expect(requestGatewayMock.mock.calls.filter(([method]) => method === 'session.active_list')).toHaveLength(1)
    )

    hook.rerender({ connectionId: 'source-b' })
    await act(async () => Promise.resolve())

    resolveStaleList({
      sessions: [{ id: staleRuntimeId, session_key: staleStoredId, status: 'working' }]
    })
    await act(async () => staleList)

    expect($sessionStates.get()[staleRuntimeId]).toBeUndefined()
  })

  it('ignores a delayed empty active-list snapshot after the tile starts a newer turn', async () => {
    const tileRuntimeId = 'runtime-newer-than-active-list'
    const tileStoredId = 'stored-newer-than-active-list'
    let activeListCalls = 0
    let resolveStaleEmptyList!: (value: { sessions: never[] }) => void

    const staleEmptyList = new Promise<{ sessions: never[] }>(resolve => {
      resolveStaleEmptyList = resolve
    })

    const requestGatewayMock = vi.fn(async (method: string) => {
      if (method !== 'session.active_list') {
        return {}
      }

      activeListCalls += 1

      if (activeListCalls === 1) {
        return {
          sessions: [{ id: tileRuntimeId, session_key: tileStoredId, status: 'working' }]
        }
      }

      return staleEmptyList
    })

    const requestGateway = requestGatewayMock as never

    const updateSessionState = vi.fn(
      (
        runtimeId: string,
        updater: (state: ReturnType<typeof createClientSessionState>) => ReturnType<typeof createClientSessionState>
      ) => updater($sessionStates.get()[runtimeId] ?? createClientSessionState(tileStoredId))
    )

    const stable = {
      refreshActiveTranscript: vi.fn(async () => undefined),
      refreshCronJobs: vi.fn(async () => undefined),
      refreshCurrentModel: vi.fn(async () => undefined),
      refreshHermesConfig: vi.fn(async () => undefined),
      refreshMessagingSessions: vi.fn(async () => undefined),
      refreshSessions: vi.fn(async () => undefined)
    }

    $changeEventsAvailable.set(true)
    $sessionTiles.set([{ runtimeId: tileRuntimeId, storedSessionId: tileStoredId }])
    publishSessionState(tileRuntimeId, {
      ...createClientSessionState(tileStoredId),
      awaitingResponse: true,
      busy: true,
      turnLive: true
    })
    vi.mocked(getLatestSessionMessages).mockResolvedValue(transcript('stale persisted answer', tileStoredId) as never)

    renderHook(() =>
      useBackgroundSync({
        activeConnectionId: 'local',
        activeGatewayProfile: 'default',
        activeIsMessaging: false,
        activeSessionId: null,
        activeStoredSessionId: null,
        freshDraftReady: false,
        gatewayState: 'open',
        requestGateway,
        updateSessionState,
        ...stable
      })
    )

    await waitFor(() =>
      expect(requestGatewayMock.mock.calls.filter(([method]) => method === 'session.active_list')).toHaveLength(1)
    )

    act(() => notifySessionsChanged())
    await waitFor(() =>
      expect(requestGatewayMock.mock.calls.filter(([method]) => method === 'session.active_list')).toHaveLength(2)
    )

    const newerState = {
      ...createClientSessionState(tileStoredId),
      awaitingResponse: true,
      busy: true,
      streamId: 'newer-stream',
      turnLive: true,
      turnStartedAt: Date.now()
    }

    publishSessionState(tileRuntimeId, newerState)
    resolveStaleEmptyList({ sessions: [] })
    await act(async () => staleEmptyList)

    expect($sessionStates.get()[tileRuntimeId]).toBe(newerState)
    expect(getLatestSessionMessages).toHaveBeenCalledOnce()
    expect(updateSessionState).toHaveBeenCalledOnce()
  })

  it('does not replace a tile while it is awaiting its first response', async () => {
    const tileRuntimeId = 'runtime-awaiting-bot-tile'
    const tileStoredId = 'stored-awaiting-bot-tile'

    publishSessionState(tileRuntimeId, {
      ...createClientSessionState(tileStoredId),
      awaitingResponse: true,
      busy: false,
      messages: [{ id: 'user-live', parts: [{ text: 'question', type: 'text' }], role: 'user' }]
    })
    vi.mocked(getLatestSessionMessages).mockResolvedValue({
      messages: [{ content: 'question', role: 'user', timestamp: 1 }],
      session_id: tileStoredId
    } as never)

    const updateSessionState = vi.fn(
      (
        runtimeId: string,
        updater: (state: ReturnType<typeof createClientSessionState>) => ReturnType<typeof createClientSessionState>
      ) => {
        const next = updater($sessionStates.get()[runtimeId] ?? createClientSessionState(tileStoredId))

        publishSessionState(runtimeId, next)

        return next
      }
    )

    await reconcileTileTranscriptsForTest({
      tiles: [{ storedSessionId: tileStoredId, runtimeId: tileRuntimeId }],
      requestSequenceRef: { current: 0 },
      signatureRef: { current: new Map<string, string>() },
      updateSessionState
    })

    expect(getLatestSessionMessages).toHaveBeenCalledWith(tileStoredId)
    expect(updateSessionState).toHaveBeenCalledOnce()
    expect($sessionStates.get()[tileRuntimeId]).toMatchObject({ awaitingResponse: true, busy: false })
    expect($sessionStates.get()[tileRuntimeId]?.messages.at(-1)?.role).toBe('user')
  })

  it('does not replace a tile while its turn is live after transient busy flags settle', async () => {
    const tileRuntimeId = 'runtime-live-bot-tile'
    const tileStoredId = 'stored-live-bot-tile'

    let canonicalState: ReturnType<typeof createClientSessionState> = {
      ...createClientSessionState(tileStoredId),
      messages: [
        { id: 'user-live', parts: [{ text: 'question', type: 'text' }], role: 'user' },
        {
          id: 'assistant-stream-live',
          parts: [{ text: 'streaming answer', type: 'text' }],
          pending: true,
          role: 'assistant'
        }
      ],
      turnLive: true
    }

    publishSessionState(tileRuntimeId, canonicalState)
    vi.mocked(getLatestSessionMessages).mockResolvedValue({
      messages: [{ content: 'question', role: 'user', timestamp: 1 }],
      session_id: tileStoredId
    } as never)

    const updateSessionState = vi.fn(
      (
        _runtimeId: string,
        updater: (state: ReturnType<typeof createClientSessionState>) => ReturnType<typeof createClientSessionState>
      ) => {
        canonicalState = updater(canonicalState)
        publishSessionState(tileRuntimeId, canonicalState)

        return canonicalState
      }
    )

    await reconcileTileTranscriptsForTest({
      tiles: [{ storedSessionId: tileStoredId, runtimeId: tileRuntimeId }],
      requestSequenceRef: { current: 0 },
      signatureRef: { current: new Map<string, string>() },
      updateSessionState
    })

    expect(getLatestSessionMessages).toHaveBeenCalledWith(tileStoredId)
    expect(updateSessionState).toHaveBeenCalledOnce()
    expect(canonicalState).toMatchObject({ turnLive: true })
    expect(canonicalState.messages.at(-1)?.parts[0]).toMatchObject({
      text: 'streaming answer'
    })
  })

  it('does not replace a tile while it is blocked on user input', async () => {
    const tileRuntimeId = 'runtime-needs-input-bot-tile'
    const tileStoredId = 'stored-needs-input-bot-tile'

    publishSessionState(tileRuntimeId, {
      ...createClientSessionState(tileStoredId),
      busy: false,
      needsInput: true,
      messages: [{ id: 'assistant-question', parts: [{ text: 'Choose an option', type: 'text' }], role: 'assistant' }]
    })

    const updateSessionState = vi.fn()

    await reconcileTileTranscriptsForTest({
      tiles: [{ storedSessionId: tileStoredId, runtimeId: tileRuntimeId }],
      requestSequenceRef: { current: 0 },
      signatureRef: { current: new Map<string, string>() },
      updateSessionState
    })

    expect(getLatestSessionMessages).not.toHaveBeenCalled()
    expect(updateSessionState).not.toHaveBeenCalled()
  })

  it('discards an in-flight tile snapshot when that tile starts streaming', async () => {
    const tileRuntimeId = 'runtime-tile-starts-streaming'
    const tileStoredId = 'stored-tile-starts-streaming'
    let resolveSnapshot!: (value: ReturnType<typeof transcript>) => void

    const snapshot = new Promise<ReturnType<typeof transcript>>(resolve => {
      resolveSnapshot = resolve
    })

    $sessionTiles.set([{ storedSessionId: tileStoredId, runtimeId: tileRuntimeId }])
    publishSessionState(tileRuntimeId, createClientSessionState(tileStoredId))
    const updateSessionState = vi.fn()
    const signatureRef = { current: new Map<string, string>() }

    vi.mocked(getLatestSessionMessages).mockResolvedValue(transcript('last accepted answer', tileStoredId) as never)
    await reconcileTileTranscriptsForTest({
      requestSequenceRef: { current: 0 },
      signatureRef,
      updateSessionState
    })
    const [signatureKey] = signatureRef.current.keys()
    const lastAcceptedSignature = signatureRef.current.get(signatureKey)

    updateSessionState.mockClear()
    vi.mocked(getLatestSessionMessages).mockReturnValue(snapshot as never)

    const reconciliation = reconcileTileTranscriptsForTest({
      requestSequenceRef: { current: 0 },
      signatureRef,
      updateSessionState
    })

    await waitFor(() => expect(getLatestSessionMessages).toHaveBeenLastCalledWith(tileStoredId))

    publishSessionState(tileRuntimeId, {
      ...createClientSessionState(tileStoredId),
      awaitingResponse: true,
      busy: true,
      messages: [
        { id: 'user-live', parts: [{ text: 'question', type: 'text' }], role: 'user' },
        { id: 'assistant-live', parts: [{ text: 'new stream', type: 'text' }], role: 'assistant' }
      ]
    })
    resolveSnapshot(transcript('stale persisted answer', tileStoredId))
    await reconciliation

    expect(updateSessionState).not.toHaveBeenCalled()
    expect(signatureRef.current.get(signatureKey)).toBe(lastAcceptedSignature)
  })

  it('discards a snapshot when a complete tile turn starts and finishes during the read', async () => {
    const tileRuntimeId = 'runtime-tile-completes-during-read'
    const tileStoredId = 'stored-tile-completes-during-read'
    let resolveSnapshot!: (value: ReturnType<typeof transcript>) => void

    const snapshot = new Promise<ReturnType<typeof transcript>>(resolve => {
      resolveSnapshot = resolve
    })

    $sessionTiles.set([{ storedSessionId: tileStoredId, runtimeId: tileRuntimeId }])
    publishSessionState(tileRuntimeId, createClientSessionState(tileStoredId))
    vi.mocked(getLatestSessionMessages).mockReturnValue(snapshot as never)
    const updateSessionState = vi.fn()

    const reconciliation = reconcileTileTranscriptsForTest({
      requestSequenceRef: { current: 0 },
      signatureRef: { current: new Map<string, string>() },
      updateSessionState
    })

    await waitFor(() => expect(getLatestSessionMessages).toHaveBeenCalledWith(tileStoredId))

    publishSessionState(tileRuntimeId, {
      ...createClientSessionState(tileStoredId),
      awaitingResponse: true,
      busy: true,
      messages: [{ id: 'user-new', parts: [{ text: 'new question', type: 'text' }], role: 'user' }]
    })
    publishSessionState(tileRuntimeId, {
      ...createClientSessionState(tileStoredId),
      messages: [
        { id: 'user-new', parts: [{ text: 'new question', type: 'text' }], role: 'user' },
        { id: 'assistant-new', parts: [{ text: 'new completed answer', type: 'text' }], role: 'assistant' }
      ]
    })
    resolveSnapshot(transcript('stale pre-turn answer', tileStoredId))
    await reconciliation

    expect(updateSessionState).not.toHaveBeenCalled()
  })

  it('discards a late snapshot after the tile closes', async () => {
    const tileRuntimeId = 'runtime-tile-closes-during-read'
    const tileStoredId = 'stored-tile-closes-during-read'
    let resolveSnapshot!: (value: ReturnType<typeof transcript>) => void

    const snapshot = new Promise<ReturnType<typeof transcript>>(resolve => {
      resolveSnapshot = resolve
    })

    $sessionTiles.set([{ storedSessionId: tileStoredId, runtimeId: tileRuntimeId }])
    publishSessionState(tileRuntimeId, createClientSessionState(tileStoredId))
    const updateSessionState = vi.fn()
    const signatureRef = { current: new Map<string, string>() }

    vi.mocked(getLatestSessionMessages).mockResolvedValue(transcript('last accepted answer', tileStoredId) as never)
    await reconcileTileTranscriptsForTest({
      requestSequenceRef: { current: 0 },
      signatureRef,
      updateSessionState
    })
    const [signatureKey] = signatureRef.current.keys()

    updateSessionState.mockClear()
    vi.mocked(getLatestSessionMessages).mockReturnValue(snapshot as never)

    const reconciliation = reconcileTileTranscriptsForTest({
      requestSequenceRef: { current: 0 },
      signatureRef,
      updateSessionState
    })

    await waitFor(() => expect(getLatestSessionMessages).toHaveBeenCalledTimes(2))

    $sessionTiles.set([])
    resolveSnapshot(transcript('late snapshot', tileStoredId))
    await reconciliation

    expect(updateSessionState).not.toHaveBeenCalled()
    expect(signatureRef.current.has(signatureKey)).toBe(false)
  })

  it('discards a late snapshot after the tile owner generation changes', async () => {
    const tileRuntimeId = 'runtime-tile-generation-rebound'
    const tileStoredId = 'stored-tile-generation-rebound'
    let resolveSnapshot!: (value: ReturnType<typeof transcript>) => void

    const snapshot = new Promise<ReturnType<typeof transcript>>(resolve => {
      resolveSnapshot = resolve
    })

    $sessionTiles.set([{ ownerGeneration: 1, storedSessionId: tileStoredId, runtimeId: tileRuntimeId }])
    publishSessionState(tileRuntimeId, createClientSessionState(tileStoredId))
    vi.mocked(getLatestSessionMessages).mockReturnValue(snapshot as never)
    const updateSessionState = vi.fn()

    const reconciliation = reconcileTileTranscriptsForTest({
      requestSequenceRef: { current: 0 },
      signatureRef: { current: new Map<string, string>() },
      updateSessionState
    })

    await waitFor(() => expect(getLatestSessionMessages).toHaveBeenCalledWith(tileStoredId))

    $sessionTiles.set([{ ownerGeneration: 2, storedSessionId: tileStoredId, runtimeId: tileRuntimeId }])
    resolveSnapshot(transcript('old generation snapshot', tileStoredId))
    await reconciliation

    expect(updateSessionState).not.toHaveBeenCalled()
  })

  it('discards a late tile snapshot after that runtime becomes the main session', async () => {
    const tileRuntimeId = 'runtime-tile-promoted-during-read'
    const tileStoredId = 'stored-tile-promoted-during-read'
    let resolveSnapshot!: (value: ReturnType<typeof transcript>) => void

    const snapshot = new Promise<ReturnType<typeof transcript>>(resolve => {
      resolveSnapshot = resolve
    })

    $sessionTiles.set([{ storedSessionId: tileStoredId, runtimeId: tileRuntimeId }])
    publishSessionState(tileRuntimeId, createClientSessionState(tileStoredId))
    vi.mocked(getLatestSessionMessages).mockReturnValue(snapshot as never)
    const updateSessionState = vi.fn()

    const reconciliation = reconcileTileTranscriptsForTest({
      requestSequenceRef: { current: 0 },
      signatureRef: { current: new Map<string, string>() },
      updateSessionState
    })

    await waitFor(() => expect(getLatestSessionMessages).toHaveBeenCalledWith(tileStoredId))

    $activeSessionId.set(tileRuntimeId)
    resolveSnapshot(transcript('late tile snapshot', tileStoredId))
    await reconciliation

    expect(updateSessionState).not.toHaveBeenCalled()
  })

  it('hydrates the same persisted snapshot after a tile rebinds to a new runtime generation', async () => {
    const tileStoredId = 'stored-tile-rebound-after-accepted-snapshot'
    const firstRuntimeId = 'runtime-tile-first-binding'
    const secondRuntimeId = 'runtime-tile-second-binding'
    const signatureRef = { current: new Map<string, string>() }

    $sessionTiles.set([{ ownerGeneration: 1, runtimeId: firstRuntimeId, storedSessionId: tileStoredId }])
    publishSessionState(firstRuntimeId, createClientSessionState(tileStoredId))
    vi.mocked(getLatestSessionMessages).mockResolvedValue(transcript('persisted answer', tileStoredId) as never)

    const updateSessionState = vi.fn(
      (
        runtimeId: string,
        updater: (state: ReturnType<typeof createClientSessionState>) => ReturnType<typeof createClientSessionState>
      ) => updater($sessionStates.get()[runtimeId] ?? createClientSessionState(tileStoredId))
    )

    const args = { requestSequenceRef: { current: 0 }, signatureRef, updateSessionState }

    await reconcileTileTranscriptsForTest(args)

    $sessionTiles.set([{ ownerGeneration: 2, runtimeId: secondRuntimeId, storedSessionId: tileStoredId }])
    publishSessionState(secondRuntimeId, createClientSessionState(tileStoredId))
    await reconcileTileTranscriptsForTest(args)

    expect(updateSessionState.mock.calls.map(([runtimeId]) => runtimeId)).toEqual([firstRuntimeId, secondRuntimeId])
    expect(signatureRef.current.size).toBe(1)
  })

  it('skips the tile update when the persisted signature is unchanged', async () => {
    $changeEventsAvailable.set(true)

    const TILE_RUNTIME_ID = 'runtime-tile-2'
    const TILE_STORED_ID = 'stored-tile-2'

    publishSessionState(TILE_RUNTIME_ID, createClientSessionState(TILE_STORED_ID))

    const signatureRef = { current: new Map<string, string>() }

    const pre = {
      messages: [
        { content: 'q', role: 'user', timestamp: 1 },
        { content: 'a', role: 'assistant', timestamp: 2 }
      ],
      session_id: TILE_STORED_ID
    }

    vi.mocked(getLatestSessionMessages).mockResolvedValue(pre as never)
    const updateSessionState = vi.fn()
    const requestSequenceRef = { current: 0 }

    const args = {
      tiles: [{ storedSessionId: TILE_STORED_ID, runtimeId: TILE_RUNTIME_ID }],
      requestSequenceRef,
      signatureRef,
      updateSessionState
    }

    await act(async () => reconcileTileTranscriptsForTest(args))
    expect(updateSessionState).toHaveBeenCalledOnce()

    updateSessionState.mockClear()
    await act(async () => reconcileTileTranscriptsForTest(args))

    expect(getLatestSessionMessages).toHaveBeenCalledTimes(2)
    expect(updateSessionState).not.toHaveBeenCalled()
  })

  it('refreshes a local/Desktop session when sessions.changed ticks', async () => {
    $changeEventsAvailable.set(true)
    $activeSessionId.set(ACTIVE_RUNTIME_ID)
    $selectedStoredSessionId.set(ACTIVE_STORED_ID)
    setSessionOwnerHint(ACTIVE_STORED_ID, {
      connectionId: 'stale-owner',
      mode: 'remote',
      profile: 'wrong-profile',
      targetProfile: 'wrong-target'
    })
    setSessions([
      {
        connectionId: 'future-visible-owner',
        id: ACTIVE_STORED_ID,
        profile: 'desktop-profile',
        source: 'desktop',
        targetProfile: 'must-not-rewrite-visible-row'
      } as never
    ])
    const fixture = makeRefresh(resolveActiveTranscriptSession)
    vi.mocked(getLatestSessionMessages).mockResolvedValue(transcript('external answer') as never)

    renderSync(fixture.refresh)

    act(() => notifySessionsChanged())

    await waitFor(() =>
      expect(fixture.states.get(ACTIVE_RUNTIME_ID)?.messages.at(-1)?.parts[0]).toMatchObject({
        text: 'external answer'
      })
    )
    expect(getLatestSessionMessages).toHaveBeenCalledWith(ACTIVE_STORED_ID, 'desktop-profile')
  })

  it('does not add a periodic transcript poll to local/Desktop sessions', async () => {
    vi.useFakeTimers()
    $changeEventsAvailable.set(true)
    const refresh = vi.fn(async () => undefined)

    renderSync(refresh)
    expect(refresh).not.toHaveBeenCalled()

    await act(async () => {
      vi.advanceTimersByTime(60_000)
      await Promise.resolve()
    })

    expect(refresh).not.toHaveBeenCalled()
  })

  it('retains the existing periodic backstop for messaging sessions', async () => {
    vi.useFakeTimers()
    $changeEventsAvailable.set(true)
    const refresh = vi.fn(async () => undefined)

    renderSync(refresh, { activeIsMessaging: true })
    expect(refresh).toHaveBeenCalledTimes(1)
    await act(async () => Promise.resolve())
    refresh.mockClear()

    await act(async () => {
      vi.advanceTimersByTime(30_000)
      await Promise.resolve()
    })

    expect(refresh).toHaveBeenCalledTimes(1)
  })

  it('only defers an external tick while busy, then refreshes once after idle', async () => {
    $changeEventsAvailable.set(true)
    setBusy(true)
    const refresh = vi.fn(async () => undefined)

    renderSync(refresh)

    act(() => setBusy(false))
    expect(refresh).not.toHaveBeenCalled()
    act(() => setBusy(true))

    act(() => {
      notifySessionsChanged()
      notifySessionsChanged()
    })
    expect(refresh).not.toHaveBeenCalled()

    act(() => setBusy(false))
    await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1))
  })

  it('coalesces a burst of global session-change ticks', async () => {
    vi.useFakeTimers()
    $changeEventsAvailable.set(true)
    const refresh = vi.fn(async () => undefined)

    renderSync(refresh)

    act(() => {
      for (let index = 0; index < 20; index += 1) {
        notifySessionsChanged()
      }
    })
    expect(refresh).toHaveBeenCalledTimes(1)

    await act(async () => {
      vi.advanceTimersByTime(9_999)
      await Promise.resolve()
    })

    expect(refresh).toHaveBeenCalledTimes(1)
  })
})

describe('reconcileActiveTranscript', () => {
  it('resolves and hydrates a messaging session from the messaging sessions store', async () => {
    setSessionOwnerHint(ACTIVE_STORED_ID, {
      connectionId: 'stale-messaging-owner',
      mode: 'remote',
      profile: 'wrong-profile',
      targetProfile: 'wrong-target'
    })
    setMessagingSessions([{ id: ACTIVE_STORED_ID, profile: 'messaging-profile', source: 'telegram' } as never])
    const fixture = makeRefresh(resolveActiveTranscriptSession)
    vi.mocked(getLatestSessionMessages).mockResolvedValue(transcript('telegram answer') as never)

    await fixture.refresh()

    expect(getLatestSessionMessages).toHaveBeenCalledWith(ACTIVE_STORED_ID, 'messaging-profile')
    expect(fixture.states.get(ACTIVE_RUNTIME_ID)?.messages.at(-1)?.parts[0]).toMatchObject({
      text: 'telegram answer'
    })
  })

  it('fails closed when a hidden session id has multiple owner hints', async () => {
    const ambiguousStoredSessionId = 'ambiguous-hidden-chat'
    setSessionOwnerHint(ambiguousStoredSessionId, {
      connectionId: 'owner-a',
      mode: 'remote',
      profile: 'bot'
    })
    setSessionOwnerHint(ambiguousStoredSessionId, {
      connectionId: 'owner-b',
      mode: 'remote',
      profile: 'bot'
    })
    const fixture = makeRefresh(resolveActiveTranscriptSession)
    fixture.selectedStoredSessionIdRef.current = ambiguousStoredSessionId

    await fixture.refresh()

    expect(getLatestSessionMessages).not.toHaveBeenCalled()
    expect(fixture.updateSessionState).not.toHaveBeenCalled()
  })

  it('uses the presentation profile when a hidden owner has no target profile', async () => {
    const hiddenStoredSessionId = 'hidden-no-target'
    setSessionOwnerHint(hiddenStoredSessionId, {
      connectionId: 'owner-no-target',
      mode: 'remote',
      profile: 'presentation-profile'
    })
    const fixture = makeRefresh(resolveActiveTranscriptSession)
    fixture.selectedStoredSessionIdRef.current = hiddenStoredSessionId

    await fixture.refresh()

    expect(getLatestSessionMessages).toHaveBeenCalledWith(hiddenStoredSessionId, {
      connectionId: 'owner-no-target',
      profile: 'presentation-profile'
    })
  })

  it('reads and publishes only the active hidden owner when another owner coexists', async () => {
    const ownerAStoredSessionId = 'owner-a-chat'
    const ownerBStoredSessionId = 'owner-b-hidden-chat'

    const ownerBRoute = {
      connectionId: 'owner-b',
      mode: 'remote' as const,
      profile: 'bot-route',
      targetProfile: 'bot-b'
    }

    setSessions([{ id: ownerAStoredSessionId, profile: 'bot-a', source: 'desktop' } as never])
    setSessionOwnerHint(ownerAStoredSessionId, {
      connectionId: 'owner-a',
      mode: 'remote',
      profile: 'bot-route',
      targetProfile: 'bot-a'
    })
    setSessionOwnerHint(ownerBStoredSessionId, ownerBRoute)
    const fixture = makeRefresh(resolveActiveTranscriptSession)
    fixture.selectedStoredSessionIdRef.current = ownerBStoredSessionId
    vi.mocked(getLatestSessionMessages).mockResolvedValue(transcript('owner B answer', ownerBStoredSessionId) as never)

    await fixture.refresh()

    expect(getLatestSessionMessages).toHaveBeenCalledTimes(1)
    expect(getLatestSessionMessages).toHaveBeenCalledWith(ownerBStoredSessionId, {
      connectionId: ownerBRoute.connectionId,
      profile: ownerBRoute.targetProfile
    })
    expect(fixture.updateSessionState).toHaveBeenCalledWith(
      ACTIVE_RUNTIME_ID,
      expect.any(Function),
      ownerBStoredSessionId
    )
    expect(fixture.states.get(ACTIVE_RUNTIME_ID)?.messages.at(-1)?.parts[0]).toMatchObject({
      text: 'owner B answer'
    })
  })

  it('publishes changed authoritative messages once without duplicates', async () => {
    const fixture = makeRefresh()
    vi.mocked(getLatestSessionMessages).mockResolvedValue(transcript('new answer') as never)

    await fixture.refresh()

    expect(fixture.updateSessionState).toHaveBeenCalledTimes(1)
    const messages = fixture.states.get(ACTIVE_RUNTIME_ID)?.messages ?? []
    expect(messages.map(message => message.role)).toEqual(['user', 'assistant'])
    expect(new Set(messages.map(message => message.id)).size).toBe(messages.length)

    await fixture.refresh()

    expect(fixture.updateSessionState).toHaveBeenCalledTimes(1)
  })

  it('preserves a local assistant error while hydrating authoritative messages', async () => {
    const fixture = makeRefresh()
    fixture.state.messages = [
      { id: '1-0-user', parts: [{ text: 'question', type: 'text' }], role: 'user' },
      { error: 'local failure', id: 'assistant-error', parts: [], role: 'assistant' }
    ]
    vi.mocked(getLatestSessionMessages).mockResolvedValue({
      messages: [{ content: 'question', role: 'user', timestamp: 1 }],
      session_id: ACTIVE_STORED_ID
    } as never)

    await fixture.refresh()

    const messages = fixture.states.get(ACTIVE_RUNTIME_ID)?.messages ?? []
    expect(messages.map(message => message.id)).toEqual(['1-0-user', 'assistant-error'])
    expect(messages.at(-1)?.error).toBe('local failure')
  })

  it('does not clobber a busy stream', async () => {
    const fixture = makeRefresh()
    fixture.busyRef.current = true

    await fixture.refresh()

    expect(getLatestSessionMessages).not.toHaveBeenCalled()
    expect(fixture.updateSessionState).not.toHaveBeenCalled()
  })

  it('discards a response when the active session changes in flight', async () => {
    const fixture = makeRefresh()
    let resolve: ((value: unknown) => void) | undefined
    vi.mocked(getLatestSessionMessages).mockReturnValueOnce(
      new Promise(currentResolve => {
        resolve = currentResolve
      }) as never
    )

    const request = fixture.refresh()
    fixture.selectedStoredSessionIdRef.current = 'stored-other'
    fixture.activeSessionIdRef.current = 'runtime-other'
    resolve?.(transcript('stale answer'))
    await request

    expect(fixture.updateSessionState).not.toHaveBeenCalled()
  })
})

describe('windowIsActivelyViewed', () => {
  it('requires both DOM visibility and keyboard focus', () => {
    expect(windowIsActivelyViewed({ focused: true, visibilityState: 'visible' })).toBe(true)
    expect(windowIsActivelyViewed({ focused: false, visibilityState: 'visible' })).toBe(false)
    expect(windowIsActivelyViewed({ focused: true, visibilityState: 'hidden' })).toBe(false)
  })
})

describe('rehydrateLiveSessionStatuses', () => {
  it('restores running sessions after reconnect without opening them', () => {
    const now = 1_800_000_000_000

    rehydrateLiveSessionStatuses(
      {
        sessions: [
          {
            id: 'runtime-overnight',
            last_active: (now - SESSION_WATCHDOG_TIMEOUT_MS - 1_000) / 1000,
            session_key: 'overnight-exam-learning',
            status: 'working'
          },
          {
            id: 'runtime-cleanup',
            last_active: now / 1000,
            session_key: 'temporary-file-cleanup',
            status: 'working'
          }
        ]
      },
      now
    )

    expect($workingSessionIds.get()).toEqual(['overnight-exam-learning', 'temporary-file-cleanup'])
    expect($stalledSessionIds.get()).toEqual(['overnight-exam-learning'])
    expect($attentionSessionIds.get()).toEqual([])
  })

  it('restores a waiting turn as working and needing attention', () => {
    rehydrateLiveSessionStatuses({
      sessions: [{ id: 'runtime-needs-user', session_key: 'needs-user', status: 'waiting' }]
    })

    expect($workingSessionIds.get()).toEqual(['needs-user'])
    expect($attentionSessionIds.get()).toEqual(['needs-user'])
    expect($stalledSessionIds.get()).toEqual([])
  })

  it('preserves an accepted optimistic turn while active-list reports starting', () => {
    const runtimeId = 'runtime-starting-accepted'
    const storedId = 'stored-starting-accepted'

    const accepted = {
      ...createClientSessionState(storedId),
      awaitingResponse: true,
      busy: true,
      streamId: 'accepted-stream',
      turnStartedAt: Date.now() - PRE_TURN_LIVE_SETTLE_GRACE_MS - 1
    }

    publishSessionState(runtimeId, accepted)
    rehydrateLiveSessionStatuses(
      { sessions: [{ id: runtimeId, session_key: storedId, status: 'starting' }] },
      Date.now(),
      'starting-accepted-scope',
      $sessionStates.get()
    )

    expect($sessionStates.get()[runtimeId]).toBe(accepted)
  })

  it('does not let a current working row re-arm an interrupted runtime', () => {
    const runtimeId = 'runtime-interrupted-before-status'
    const storedId = 'stored-interrupted-before-status'

    const interrupted = {
      ...createClientSessionState(storedId),
      interrupted: true
    }

    publishSessionState(runtimeId, interrupted)
    rehydrateLiveSessionStatuses(
      { sessions: [{ id: runtimeId, session_key: storedId, status: 'working' }] },
      Date.now(),
      'interrupted-scope',
      $sessionStates.get()
    )

    expect($sessionStates.get()[runtimeId]).toBe(interrupted)
    expect($sessionStates.get()[runtimeId]).toMatchObject({ busy: false, interrupted: true, turnLive: false })
  })

  it('reaps a turnLive-only runtime when it disappears from active-list', () => {
    const runtimeId = 'runtime-turn-live-only'
    const storedId = 'stored-turn-live-only'
    const scopeKey = 'turn-live-only-scope'

    rehydrateLiveSessionStatuses(
      {
        sessions: [{ id: runtimeId, session_key: storedId, status: 'working' }]
      },
      Date.now(),
      scopeKey
    )
    publishSessionState(runtimeId, {
      ...createClientSessionState(storedId),
      turnLive: true
    })

    expect(rehydrateLiveSessionStatuses({ sessions: [] }, Date.now(), scopeKey, $sessionStates.get())).toBe(true)
    expect($sessionStates.get()[runtimeId]).toMatchObject({ busy: false, turnLive: false })
  })

  it('preserves a fresh optimistic turn across a current idle active-list row', () => {
    const now = 1_800_000_000_000
    const runtimeId = 'runtime-fresh-optimistic'
    const storedId = 'stored-fresh-optimistic'

    const optimistic = {
      ...createClientSessionState(storedId),
      awaitingResponse: true,
      busy: true,
      streamId: 'pending-stream',
      turnStartedAt: now - PRE_TURN_LIVE_SETTLE_GRACE_MS + 1
    }

    publishSessionState(runtimeId, optimistic)

    expect(
      rehydrateLiveSessionStatuses(
        { sessions: [{ id: runtimeId, session_key: storedId, status: 'idle' }] },
        now,
        'fresh-optimistic-scope',
        $sessionStates.get()
      )
    ).toBe(false)
    expect($sessionStates.get()[runtimeId]).toBe(optimistic)
  })

  it('settles an optimistic turn once the pre-start grace expires', () => {
    const now = 1_800_000_000_000
    const runtimeId = 'runtime-expired-optimistic'
    const storedId = 'stored-expired-optimistic'

    publishSessionState(runtimeId, {
      ...createClientSessionState(storedId),
      awaitingResponse: true,
      busy: true,
      streamId: 'pending-stream',
      turnStartedAt: now - PRE_TURN_LIVE_SETTLE_GRACE_MS - 1
    })

    expect(
      rehydrateLiveSessionStatuses(
        { sessions: [{ id: runtimeId, session_key: storedId, status: 'idle' }] },
        now,
        'expired-optimistic-scope',
        $sessionStates.get()
      )
    ).toBe(true)
    expect($sessionStates.get()[runtimeId]).toMatchObject({
      awaitingResponse: false,
      busy: false,
      streamId: null,
      turnLive: false,
      turnStartedAt: null
    })
  })

  it('keeps a stale absence eligible for a later current recovery snapshot', () => {
    const now = 1_800_000_000_000
    const runtimeId = 'runtime-stale-absence'
    const storedId = 'stored-stale-absence'
    const scopeKey = 'stale-absence-scope'

    rehydrateLiveSessionStatuses(
      { sessions: [{ id: runtimeId, session_key: storedId, status: 'working' }] },
      now,
      scopeKey
    )
    const staleRequestState = $sessionStates.get()

    const newerState = {
      ...createClientSessionState(storedId),
      awaitingResponse: true,
      busy: true,
      streamId: 'newer-stream',
      turnLive: true,
      turnStartedAt: now + 1
    }

    publishSessionState(runtimeId, newerState)

    expect(rehydrateLiveSessionStatuses({ sessions: [] }, now + 2, scopeKey, staleRequestState)).toBe(false)
    expect($sessionStates.get()[runtimeId]).toBe(newerState)

    const currentRequestState = $sessionStates.get()

    expect(rehydrateLiveSessionStatuses({ sessions: [] }, now + 3, scopeKey, currentRequestState)).toBe(true)
    expect($sessionStates.get()[runtimeId]).toMatchObject({
      awaitingResponse: false,
      busy: false,
      streamId: null,
      turnLive: false,
      turnStartedAt: null
    })
  })

  it('keeps idle, starting, and malformed rows out of working state without local turn proof', () => {
    rehydrateLiveSessionStatuses({
      sessions: [
        { id: 'runtime-idle', session_key: 'idle-session', status: 'idle' },
        { id: 'runtime-starting', session_key: 'starting-session', status: 'starting' },
        { id: 'runtime-malformed', status: 'working' }
      ]
    })

    expect($workingSessionIds.get()).toEqual([])
    expect($attentionSessionIds.get()).toEqual([])
    expect($stalledSessionIds.get()).toEqual([])
  })
})

describe('typing-aware sessions.changed deferral', () => {
  // Dedicated harness: the sessions-list spy must be the exact fn handed to
  // the hook (the shared harness above wires inner vi.fn()s and its outer spy
  // observes the transcript path instead), and EVERY param must keep a stable
  // identity across the tick-driven re-renders — an unstable prop would
  // re-run the connect-reseed effect and re-subscribe the throttle each
  // render, polluting the counts under observation.
  function renderTypingSync(refreshSessions: () => Promise<void>) {
    const stable = {
      refreshActiveTranscript: async () => undefined,
      refreshCronJobs: vi.fn(),
      refreshCurrentModel: vi.fn(),
      refreshHermesConfig: vi.fn(),
      refreshMessagingSessions: vi.fn(),
      requestGateway: vi.fn(async () => ({ sessions: [] })) as never,
      // Required by the hook's params. This harness never drives the
      // transcript path, so the updater just runs against a throwaway state —
      // but it must live in `stable` like every other prop, since a fresh
      // identity per render would re-run the connect-reseed effect.
      updateSessionState: vi.fn(
        (
          _sessionId: string,
          updater: (state: ReturnType<typeof createClientSessionState>) => ReturnType<typeof createClientSessionState>
        ) => updater(createClientSessionState(ACTIVE_STORED_ID))
      )
    }

    return renderHook(() => {
      useBackgroundSync({
        activeConnectionId: 'local',
        activeGatewayProfile: 'default',
        activeIsMessaging: false,
        activeSessionId: null,
        activeStoredSessionId: null,
        freshDraftReady: false,
        gatewayState: 'open',
        ...stable,
        refreshSessions
      })
    })
  }

  const typeKey = (): void => {
    window.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'a' }))
  }

  /** Mount, land one full throttle cycle so lastRunAt sits at a known clock
   *  position, then clear the spy. */
  async function primeThrottle(refreshSessions: ReturnType<typeof vi.fn>): Promise<void> {
    act(() => notifySessionsChanged())
    await act(async () => {
      // One SESSIONS_LIST_TICK_GAP_MS covers both the immediate first tick
      // and any trailing timer the burst armed.
      vi.advanceTimersByTime(10_000)
      await Promise.resolve()
    })
    refreshSessions.mockClear()
  }

  it('holds the trailing sessions.changed refresh while a typing burst is live, then lands it once after the keyboard quiets', async () => {
    vi.useFakeTimers()
    $changeEventsAvailable.set(true)
    const refreshSessions = vi.fn(async () => undefined)

    renderTypingSync(refreshSessions)
    await primeThrottle(refreshSessions)

    // A ~6s continuous burst: keys every 200ms, broadcasts every ~1s. The
    // first broadcast finds the throttle gap already elapsed (primed), so the
    // deferral engages immediately and must hold for the whole burst.
    for (let index = 0; index < 30; index += 1) {
      typeKey()

      if (index % 5 === 0) {
        act(() => notifySessionsChanged())
      }

      await act(async () => {
        vi.advanceTimersByTime(200)
        await Promise.resolve()
      })
    }

    // The heavy list pass must not have landed under the keystrokes.
    expect(refreshSessions).not.toHaveBeenCalled()

    // Last key at ~5.8s; quiet threshold elapses ~7.3s → the held pass lands
    // exactly once shortly after.
    await act(async () => {
      vi.advanceTimersByTime(2_000)
      await Promise.resolve()
    })

    expect(refreshSessions).toHaveBeenCalledTimes(1)

    // ...and nothing extra afterwards without further broadcasts — mid-burst
    // ticks must not have stacked trailing timers behind the promised pass.
    await act(async () => {
      vi.advanceTimersByTime(10_000)
      await Promise.resolve()
    })

    expect(refreshSessions).toHaveBeenCalledTimes(1)
  })

  it('holds through a burst longer than the throttle gap and lands once after the keyboard quiets', async () => {
    vi.useFakeTimers()
    $changeEventsAvailable.set(true)
    const refreshSessions = vi.fn(async () => undefined)

    renderTypingSync(refreshSessions)
    await primeThrottle(refreshSessions)

    // Keys every 200ms for ~22s — longer than SESSIONS_LIST_TICK_GAP_MS.
    // Broadcasts keep flowing; the heavy pass must not land under them.
    for (let index = 0; index < 110; index += 1) {
      typeKey()

      if (index % 10 === 0) {
        act(() => notifySessionsChanged())
      }

      await act(async () => {
        vi.advanceTimersByTime(200)
        await Promise.resolve()
      })
    }

    expect(refreshSessions).not.toHaveBeenCalled()

    await act(async () => {
      vi.advanceTimersByTime(2_000)
      await Promise.resolve()
    })

    expect(refreshSessions).toHaveBeenCalledTimes(1)

    await act(async () => {
      vi.advanceTimersByTime(10_000)
      await Promise.resolve()
    })

    expect(refreshSessions).toHaveBeenCalledTimes(1)
  })

  it('does not defer anything when the keyboard has been idle', async () => {
    vi.useFakeTimers()
    $changeEventsAvailable.set(true)
    const refreshSessions = vi.fn(async () => undefined)

    renderTypingSync(refreshSessions)
    await primeThrottle(refreshSessions)

    act(() => notifySessionsChanged())

    await act(async () => {
      vi.advanceTimersByTime(11_000)
      await Promise.resolve()
    })

    expect(refreshSessions).toHaveBeenCalledTimes(1)
  })
})

describe('isTypingBurstActive', () => {
  it('marks a burst warm for the quiet threshold and cold at it', () => {
    resetTypingActivityTracking()

    // No keyboard history → nothing to defer for.
    expect(isTypingBurstActive(1_000_000)).toBe(false)

    noteRendererKeyboardActivity(1_000_000)
    expect(isTypingBurstActive(1_000_000)).toBe(true)
    expect(isTypingBurstActive(1_000_000 + 1_499)).toBe(true)

    // Exactly one quiet threshold after the last key the keyboard is cold.
    expect(isTypingBurstActive(1_000_000 + 1_500)).toBe(false)
  })
})
