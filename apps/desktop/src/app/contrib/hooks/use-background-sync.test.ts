import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import { $changeEventsAvailable, notifySessionsChanged, resetLiveSync } from '@/store/live-sync'
import { $activeSessionId, $selectedStoredSessionId, setBusy, setMessagingSessions, setSessions } from '@/store/session'
import {
  $attentionSessionIds,
  $stalledSessionIds,
  $workingSessionIds,
  clearAllSessionStates,
  SESSION_WATCHDOG_TIMEOUT_MS
} from '@/store/session-states'

import {
  type ActiveTranscriptRefreshDeps,
  isTypingBurstActive,
  noteRendererKeyboardActivity,
  reconcileActiveTranscript,
  rehydrateLiveSessionStatuses,
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

function transcript(answer: string) {
  return {
    messages: [
      { content: 'question', role: 'user', timestamp: 1 },
      { content: answer, role: 'assistant', timestamp: 2 }
    ],
    session_id: ACTIVE_STORED_ID
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

  const updateSessionState = vi.fn((sessionId: string, updater: (value: typeof state) => typeof state) => {
    const next = updater(states.get(sessionId) ?? createClientSessionState(ACTIVE_STORED_ID))
    states.set(sessionId, next)

    return next
  })

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
  clearAllSessionStates()
  resetTypingActivityTracking()
})

describe('active transcript refresh', () => {
  beforeEach(() => {
    vi.mocked(getLatestSessionMessages).mockResolvedValue(transcript('answer') as never)
  })

  it('refreshes a local/Desktop session when sessions.changed ticks', async () => {
    $changeEventsAvailable.set(true)
    $activeSessionId.set(ACTIVE_RUNTIME_ID)
    $selectedStoredSessionId.set(ACTIVE_STORED_ID)
    setSessions([{ id: ACTIVE_STORED_ID, profile: 'desktop-profile', source: 'desktop' } as never])
    const fixture = makeRefresh(resolveActiveTranscriptSession)
    vi.mocked(getLatestSessionMessages).mockResolvedValue(transcript('external answer') as never)

    renderSync(fixture.refresh)

    act(() => notifySessionsChanged())

    await waitFor(() =>
      expect(fixture.states.get(ACTIVE_RUNTIME_ID)?.messages.at(-1)?.parts[0]).toMatchObject({
        text: 'external answer'
      })
    )
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
    setMessagingSessions([{ id: ACTIVE_STORED_ID, profile: 'messaging-profile', source: 'telegram' } as never])
    const fixture = makeRefresh(resolveActiveTranscriptSession)
    vi.mocked(getLatestSessionMessages).mockResolvedValue(transcript('telegram answer') as never)

    await fixture.refresh()

    expect(getLatestSessionMessages).toHaveBeenCalledWith(ACTIVE_STORED_ID, 'messaging-profile')
    expect(fixture.states.get(ACTIVE_RUNTIME_ID)?.messages.at(-1)?.parts[0]).toMatchObject({
      text: 'telegram answer'
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

  it('ignores idle, starting, and malformed live-session rows', () => {
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
      requestGateway: vi.fn(async () => ({ sessions: [] })) as never
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
    // deferral engages immediately and must hold for the whole burst — well
    // short of the one-gap starvation cap.
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

  it('caps the typing deferral at one throttle gap so continuous typing cannot starve list freshness', async () => {
    vi.useFakeTimers()
    $changeEventsAvailable.set(true)
    const refreshSessions = vi.fn(async () => undefined)

    renderTypingSync(refreshSessions)
    await primeThrottle(refreshSessions)

    // Keys every 200ms for ~22s straight — a burst that outlives one full
    // SESSIONS_LIST_TICK_GAP_MS of deferral. Broadcasts keep flowing.
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

    // The starvation cap forces exactly ONE mid-burst pass (~one gap after
    // the deferral began) — worst-case freshness equals the plain cadence.
    expect(refreshSessions).toHaveBeenCalledTimes(1)

    // Burst ends → the still-held pass lands once more, then silence.
    await act(async () => {
      vi.advanceTimersByTime(2_000)
      await Promise.resolve()
    })

    expect(refreshSessions).toHaveBeenCalledTimes(2)

    await act(async () => {
      vi.advanceTimersByTime(10_000)
      await Promise.resolve()
    })

    expect(refreshSessions).toHaveBeenCalledTimes(2)
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
