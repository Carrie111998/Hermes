import { act, cleanup, render } from '@testing-library/react'
import { type MutableRefObject, useLayoutEffect } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ChatMessage } from '@/lib/chat-messages'
import {
  JOURNAL_MAX_AGE_MS,
  JOURNAL_PERSIST_THROTTLE_MS,
  JOURNAL_STORAGE_KEY,
  persistInFlightTurnState,
  readInFlightTurnJournal
} from '@/lib/inflight-turn-journal'
import {
  $activeSessionStoredIdRotation,
  $currentFastMode,
  $currentModel,
  $currentProvider,
  $currentReasoningEffort,
  $currentServiceTier,
  $messages,
  $turnStartedAt,
  setActiveSessionId,
  setActiveSessionStoredIdRotation,
  setCurrentFastMode,
  setCurrentModel,
  setCurrentProvider,
  setCurrentReasoningEffort,
  setCurrentServiceTier,
  setTurnStartedAt
} from '@/store/session'

import { useSessionStateCache } from './use-session-state-cache'

type Cache = ReturnType<typeof useSessionStateCache>

interface HarnessProps {
  activeSessionId: string | null
  onReady: (cache: Cache) => void
  selectedStoredSessionId: string | null
}

describe('useSessionStateCache — stored-id rotation provenance', () => {
  afterEach(() => {
    cleanup()
    setActiveSessionId(null)
    setActiveSessionStoredIdRotation(null)
  })

  it('emits the previous, next, and runtime ids and removes the stale reverse mapping', () => {
    let cache!: Cache

    setActiveSessionId('runtime-A')
    render(
      <Harness activeSessionId="runtime-A" onReady={value => (cache = value)} selectedStoredSessionId="stored-A" />
    )

    act(() => {
      cache.updateSessionState('runtime-A', state => state, 'stored-A')
      cache.updateSessionState('runtime-A', state => state, 'stored-A-next')
    })

    expect($activeSessionStoredIdRotation.get()).toEqual({
      nextStoredSessionId: 'stored-A-next',
      previousStoredSessionId: 'stored-A',
      runtimeSessionId: 'runtime-A'
    })
    expect(cache.runtimeIdByStoredSessionIdRef.current.has('stored-A')).toBe(false)
    expect(cache.runtimeIdByStoredSessionIdRef.current.get('stored-A-next')).toBe('runtime-A')
  })

  it('does not publish a foreground-navigation event for a background runtime rotation', () => {
    let cache!: Cache

    setActiveSessionId('runtime-B')
    render(
      <Harness activeSessionId="runtime-B" onReady={value => (cache = value)} selectedStoredSessionId="stored-B" />
    )

    act(() => {
      cache.updateSessionState('runtime-A', state => state, 'stored-A')
      cache.updateSessionState('runtime-A', state => state, 'stored-A-next')
    })

    expect($activeSessionStoredIdRotation.get()).toBeNull()
    expect(cache.runtimeIdByStoredSessionIdRef.current.has('stored-A')).toBe(false)
    expect(cache.runtimeIdByStoredSessionIdRef.current.get('stored-A-next')).toBe('runtime-A')
  })
})

function Harness({ activeSessionId, onReady, selectedStoredSessionId }: HarnessProps) {
  const busyRef: MutableRefObject<boolean> = { current: false }

  const cache = useSessionStateCache({
    activeSessionId,
    busyRef,
    selectedStoredSessionId,
    setAwaitingResponse: () => undefined,
    setBusy: () => undefined,
    setMessages: () => undefined
  })

  onReady(cache)

  return null
}

describe('useSessionStateCache — per-session turn timer', () => {
  beforeEach(() => {
    // The view-sync flush runs on a real rAF in the browser path; in jsdom we
    // want it synchronous so the global mirror is observable immediately. The
    // hook closes over `window.requestAnimationFrame`, so stub that exact ref.
    // Return null (not a handle) so the hook's `viewSyncRafRef.current = rAF(...)`
    // assignment doesn't overwrite the null the synchronous callback just set —
    // otherwise the ref reads truthy and the NEXT sync is suppressed (a real
    // browser returns a handle but runs the callback async, so this race is a
    // test-only artifact of firing synchronously).
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation((cb: FrameRequestCallback) => {
      cb(0)

      return null as unknown as number
    })
    setTurnStartedAt(null)
    setCurrentModel('')
    setCurrentProvider('')
    setCurrentReasoningEffort('')
    setCurrentServiceTier('')
    setCurrentFastMode(false)
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    setTurnStartedAt(null)
    setCurrentModel('')
    setCurrentProvider('')
    setCurrentReasoningEffort('')
    setCurrentServiceTier('')
    setCurrentFastMode(false)
  })

  it("keeps a background session's running turn clock and never mirrors it to the view", () => {
    let cache!: Cache
    // Active session is "fg-runtime"; the turn starts on the BACKGROUND session.
    render(<Harness activeSessionId="fg-runtime" onReady={c => (cache = c)} selectedStoredSessionId="fg-stored" />)

    const startedAt = 1_700_000_000_000

    act(() => {
      cache.updateSessionState('bg-runtime', state => ({ ...state, busy: true, turnStartedAt: startedAt }), 'bg-stored')
    })

    // The background session's own cache entry holds the clock...
    expect(cache.sessionStateByRuntimeIdRef.current.get('bg-runtime')?.turnStartedAt).toBe(startedAt)
    // ...but the global atom (statusbar timer) is untouched — a background turn
    // must not drive the foreground timer.
    expect($turnStartedAt.get()).toBeNull()
  })

  it("mirrors the focused session's turn clock into the global atom on view-sync", () => {
    let cache!: Cache
    render(<Harness activeSessionId="fg-runtime" onReady={c => (cache = c)} selectedStoredSessionId="fg-stored" />)

    const startedAt = 1_700_000_111_000

    // A turn on the ACTIVE session stages into the view; the flush mirrors its
    // turnStartedAt into the global atom the statusbar reads.
    act(() => {
      cache.updateSessionState('fg-runtime', state => ({ ...state, busy: true, turnStartedAt: startedAt }), 'fg-stored')
    })

    expect($turnStartedAt.get()).toBe(startedAt)
  })

  it('clears the global clock when the focused turn ends', () => {
    let cache!: Cache
    render(<Harness activeSessionId="fg-runtime" onReady={c => (cache = c)} selectedStoredSessionId="fg-stored" />)

    act(() => {
      cache.updateSessionState(
        'fg-runtime',
        state => ({ ...state, busy: true, turnStartedAt: 1_700_000_222_000 }),
        'fg-stored'
      )
    })
    expect($turnStartedAt.get()).toBe(1_700_000_222_000)

    act(() => {
      cache.updateSessionState('fg-runtime', state => ({ ...state, busy: false, turnStartedAt: null }))
    })
    expect($turnStartedAt.get()).toBeNull()
  })

  it('mirrors the focused session model metadata when switching from a cached session', () => {
    let cache!: Cache

    const { rerender } = render(
      <Harness activeSessionId="fg-runtime" onReady={c => (cache = c)} selectedStoredSessionId="fg-stored" />
    )

    act(() => {
      cache.updateSessionState(
        'bg-runtime',
        state => ({
          ...state,
          fast: true,
          model: 'anthropic/claude-opus-4.8',
          provider: 'anthropic',
          reasoningEffort: 'high',
          serviceTier: 'priority'
        }),
        'bg-stored'
      )
    })

    // Background metadata is cached but must not bleed into the visible statusbar.
    expect($currentModel.get()).toBe('')
    expect($currentReasoningEffort.get()).toBe('')
    expect($currentFastMode.get()).toBe(false)

    rerender(<Harness activeSessionId="bg-runtime" onReady={c => (cache = c)} selectedStoredSessionId="bg-stored" />)

    const bgState = cache.sessionStateByRuntimeIdRef.current.get('bg-runtime')
    expect(bgState).toBeTruthy()

    act(() => {
      cache.syncSessionStateToView('bg-runtime', bgState!)
    })

    expect($currentModel.get()).toBe('anthropic/claude-opus-4.8')
    expect($currentProvider.get()).toBe('anthropic')
    expect($currentReasoningEffort.get()).toBe('high')
    expect($currentServiceTier.get()).toBe('priority')
    expect($currentFastMode.get()).toBe(true)
  })

  it('clears stale model metadata when the newly focused session has no cached value', () => {
    setCurrentModel('previous-model')
    setCurrentProvider('previous-provider')
    setCurrentReasoningEffort('high')
    setCurrentServiceTier('priority')
    setCurrentFastMode(true)

    let cache!: Cache

    const { rerender } = render(
      <Harness activeSessionId="fg-runtime" onReady={c => (cache = c)} selectedStoredSessionId="fg-stored" />
    )

    act(() => {
      cache.updateSessionState('bg-runtime', state => ({ ...state }), 'bg-stored')
    })

    rerender(<Harness activeSessionId="bg-runtime" onReady={c => (cache = c)} selectedStoredSessionId="bg-stored" />)

    const bgState = cache.sessionStateByRuntimeIdRef.current.get('bg-runtime')
    expect(bgState).toBeTruthy()

    act(() => {
      cache.syncSessionStateToView('bg-runtime', bgState!)
    })

    expect($currentModel.get()).toBe('')
    expect($currentProvider.get()).toBe('')
    expect($currentReasoningEffort.get()).toBe('')
    expect($currentServiceTier.get()).toBe('')
    expect($currentFastMode.get()).toBe(false)
  })
})

interface LayoutProbeHarnessProps {
  activeSessionId: string | null
  onLayoutSnapshot: (snapshot: { active: string | null; selected: string | null }) => void
  onReady: (cache: Cache) => void
  selectedStoredSessionId: string | null
}

function LayoutProbeHarness({
  activeSessionId,
  onLayoutSnapshot,
  onReady,
  selectedStoredSessionId
}: LayoutProbeHarnessProps) {
  const busyRef: MutableRefObject<boolean> = { current: false }

  const cache = useSessionStateCache({
    activeSessionId,
    busyRef,
    selectedStoredSessionId,
    setAwaitingResponse: () => undefined,
    setBusy: () => undefined,
    setMessages: () => undefined
  })

  onReady(cache)

  // useLayoutEffect fires synchronously right after the DOM commit, BEFORE
  // the hook's own useEffect (a passive effect) has a chance to mirror the
  // new props into activeSessionIdRef/selectedStoredSessionIdRef. Anything
  // that reads the refs in this window — including a synchronous DOM event
  // handler firing against the just-committed view — observes the outgoing
  // session's ids.
  useLayoutEffect(() => {
    onLayoutSnapshot({
      active: cache.activeSessionIdRef.current,
      selected: cache.selectedStoredSessionIdRef.current
    })
  })

  return null
}

describe('useSessionStateCache — refs stay coherent with the committed session on switch (#59305)', () => {
  afterEach(() => cleanup())

  it('reflects the new session ids from the layout phase right after switching to a new session', () => {
    let cache!: Cache
    const snapshots: Array<{ active: string | null; selected: string | null }> = []

    const { rerender } = render(
      <LayoutProbeHarness
        activeSessionId="runtime-A"
        onLayoutSnapshot={s => snapshots.push(s)}
        onReady={c => (cache = c)}
        selectedStoredSessionId="stored-A"
      />
    )

    void cache
    snapshots.length = 0 // drop the initial-mount snapshot; only the switch matters

    rerender(
      <LayoutProbeHarness
        activeSessionId="runtime-B"
        onLayoutSnapshot={s => snapshots.push(s)}
        onReady={c => (cache = c)}
        selectedStoredSessionId="stored-B"
      />
    )

    // The refs must already reflect B by the layout phase — a callback firing
    // in this window must never observe the outgoing session's ids.
    expect(snapshots[0]).toEqual({ active: 'runtime-B', selected: 'stored-B' })
  })

  it('does not clobber an imperative ref pin on a re-render that leaves the props unchanged (#54527-class)', () => {
    // submit.ts pins activeSessionIdRef.current to a freshly resumed runtime id
    // WITHOUT updating the source atom that feeds the activeSessionId prop (by
    // design — see submit.ts's "pin the foreground session context" comment).
    // The prop-mirroring here must only fire when the prop itself changes; an
    // unconditional resync would silently undo that pin on the next incidental
    // render (wiring.tsx re-renders constantly during an active turn).
    let cache!: Cache

    const { rerender } = render(
      <Harness activeSessionId="runtime-A" onReady={c => (cache = c)} selectedStoredSessionId="stored-A" />
    )

    // Simulate submit.ts's imperative pin: a resume swapped in a new runtime
    // id without touching the prop.
    cache.activeSessionIdRef.current = 'runtime-resumed'

    // A re-render with the SAME props (e.g. an unrelated $busy/$messages
    // change elsewhere in the tree) must not touch the pinned ref.
    rerender(<Harness activeSessionId="runtime-A" onReady={c => (cache = c)} selectedStoredSessionId="stored-A" />)

    expect(cache.activeSessionIdRef.current).toBe('runtime-resumed')

    // A genuine prop change (a real navigation/selection move) still wins.
    rerender(<Harness activeSessionId="runtime-B" onReady={c => (cache = c)} selectedStoredSessionId="stored-B" />)

    expect(cache.activeSessionIdRef.current).toBe('runtime-B')
  })
})

function userMessage(id: string, text: string): ChatMessage {
  return { id, role: 'user', parts: [{ type: 'text', text }] }
}

function assistantText(id: string, text: string): ChatMessage {
  return { id, role: 'assistant', parts: [{ type: 'text', text }] }
}

function assistantError(id: string, error: string): ChatMessage {
  return { id, role: 'assistant', parts: [], error, pending: false }
}

interface ViewHarnessProps {
  activeSessionId: string | null
  onReady: (cache: Cache) => void
}

function ViewHarness({ activeSessionId, onReady }: ViewHarnessProps) {
  const busyRef: MutableRefObject<boolean> = { current: false }

  const cache = useSessionStateCache({
    activeSessionId,
    busyRef,
    selectedStoredSessionId: null,
    setAwaitingResponse: () => undefined,
    setBusy: () => undefined,
    // Wire the published view back into the real $messages atom the flush
    // reads from, so the round-trip matches production.
    setMessages: messages => $messages.set(messages)
  })

  onReady(cache)

  return null
}

describe('useSessionStateCache — cross-thread error isolation', () => {
  afterEach(() => {
    cleanup()
    $messages.set([])
  })

  it('does not leak a failed turn into another thread on switch', () => {
    $messages.set([])
    let cache!: Cache
    const { rerender } = render(<ViewHarness activeSessionId="thread-A" onReady={c => (cache = c)} />)

    // Thread A ends its turn with an out-of-funds error and is on screen.
    act(() => {
      cache.updateSessionState(
        'thread-A',
        state => ({
          ...state,
          busy: false,
          messages: [userMessage('user-a', 'do the thing'), assistantError('assistant-a-error', 'Out of funds')]
        }),
        'stored-A'
      )
    })

    expect($messages.get().some(message => message.error === 'Out of funds')).toBe(true)

    // Switch to thread B (which completed cleanly). Its cached state syncs to
    // the view while $messages still holds thread A's transcript.
    rerender(<ViewHarness activeSessionId="thread-B" onReady={c => (cache = c)} />)
    act(() => {
      cache.updateSessionState(
        'thread-B',
        state => ({
          ...state,
          busy: false,
          messages: [userMessage('user-b', 'hello'), assistantText('assistant-b', 'hi there')]
        }),
        'stored-B'
      )
    })

    expect($messages.get().map(message => message.id)).toEqual(['user-b', 'assistant-b'])
    expect($messages.get().some(message => message.error === 'Out of funds')).toBe(false)
  })

  it('still preserves a same-session local error a heartbeat dropped', () => {
    $messages.set([])
    let cache!: Cache
    render(<ViewHarness activeSessionId="thread-A" onReady={c => (cache = c)} />)

    // First paint establishes thread A as the on-screen session.
    act(() => {
      cache.updateSessionState(
        'thread-A',
        state => ({ ...state, busy: false, messages: [userMessage('user-a', 'do the thing')] }),
        'stored-A'
      )
    })

    // A local error lands in the view (e.g. failAssistantMessage wrote it).
    $messages.set([userMessage('user-a', 'do the thing'), assistantError('assistant-a-error', 'OpenRouter 403')])

    // A later same-session heartbeat carries cached state that lost the error.
    act(() => {
      cache.updateSessionState('thread-A', state => ({
        ...state,
        busy: false,
        messages: [userMessage('user-a', 'do the thing')]
      }))
    })

    expect($messages.get().some(message => message.error === 'OpenRouter 403')).toBe(true)
  })

  it('only returns a runtime whose cached state owns the requested stored session', () => {
    let cache!: Cache
    render(<Harness activeSessionId={null} onReady={value => (cache = value)} selectedStoredSessionId={null} />)

    act(() => {
      cache.ensureSessionState('runtime-A', 'stored-A')
      cache.ensureSessionState('runtime-B', 'stored-B')
    })

    expect(cache.getRuntimeIdForStoredSession('stored-A')).toBe('runtime-A')
    expect(cache.getRuntimeIdForStoredSession('missing')).toBeNull()

    // Simulate a recycled/cross-wired map entry. The reverse state ownership
    // check must reject it instead of allowing a submit into stored-B.
    cache.runtimeIdByStoredSessionIdRef.current.set('stored-A', 'runtime-B')
    expect(cache.getRuntimeIdForStoredSession('stored-A')).toBeNull()
  })
})

// ── The journal's two wiring obligations this hook owns ───────────────────────
// Both are call-site plumbing that the journal's own module tests cannot reach:
// they can prove the sweep and the rotation link WORK, never that this hook
// invokes them. Each was mutated away against the whole desktop suite and
// nothing failed before these tests existed.
describe('useSessionStateCache — in-flight turn journal wiring', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  afterEach(() => {
    cleanup()
    setActiveSessionId(null)
    window.localStorage.clear()
    vi.useRealTimers()
  })

  function tail(): ChatMessage[] {
    return [
      userMessage('u1', 'PGPASSWORD=hunter2 psql -h prod'),
      { id: 'assistant-stream-1', role: 'assistant', parts: [{ type: 'text', text: 'working' }], pending: true }
    ]
  }

  /** Write a journal entry directly, then age it past the retention window in
   *  one raw write — going through the public API would re-enter the sweep. */
  function seedExpiredEntry(storedSessionId: string): void {
    vi.useFakeTimers()
    persistInFlightTurnState({
      awaitingResponse: false,
      busy: true,
      messages: tail(),
      storedSessionId,
      streamId: 'assistant-stream-1',
      turnStartedAt: 1
    })
    vi.advanceTimersByTime(JOURNAL_PERSIST_THROTTLE_MS)
    vi.useRealTimers()

    const raw = JSON.parse(window.localStorage.getItem(JOURNAL_STORAGE_KEY)!)
    raw.entries[storedSessionId].updatedAt = Date.now() - (JOURNAL_MAX_AGE_MS + 60 * 60 * 1000)
    window.localStorage.setItem(JOURNAL_STORAGE_KEY, JSON.stringify(raw))
  }

  // The journal's load-time sweep only runs when something else calls into the
  // module. A user who launches into a fresh draft and never opens a session
  // would never sweep, which would make the retention bound — the whole
  // confidentiality argument for storing an unredacted prompt at rest —
  // contingent on unrelated activity. Mounting this hook is what makes it
  // unconditional, so mounting alone has to be enough.
  it('sweeps an expired journal entry on mount without any session being opened', () => {
    seedExpiredEntry('stored-stale')

    expect(window.localStorage.getItem(JOURNAL_STORAGE_KEY)).not.toBeNull()

    render(<Harness activeSessionId={null} onReady={() => undefined} selectedStoredSessionId={null} />)

    expect(window.localStorage.getItem(JOURNAL_STORAGE_KEY)).toBeNull()
  })

  it('leaves a live journal entry alone on mount', () => {
    vi.useFakeTimers()
    persistInFlightTurnState({
      awaitingResponse: false,
      busy: true,
      messages: tail(),
      storedSessionId: 'stored-live',
      streamId: 'assistant-stream-1',
      turnStartedAt: 1
    })
    vi.advanceTimersByTime(JOURNAL_PERSIST_THROTTLE_MS)
    vi.useRealTimers()

    render(<Harness activeSessionId={null} onReady={() => undefined} selectedStoredSessionId={null} />)

    expect(readInFlightTurnJournal('stored-live')).not.toBeNull()
  })

  // The RUNTIME id is what makes a rotated key reachable: the stored id rotates
  // mid-turn (compression forks a continuation), so without the runtime id
  // recorded on the entry, the superseded key is in neither `$sessions` nor a
  // delete's `removedIds` and NO call site can name it. If this hook stops
  // supplying it, every rotation test in the journal module still passes —
  // they pass the id explicitly — while the real app silently orphans tails.
  it('passes the runtime id to the journal, so a rotated key stays reachable', () => {
    vi.useFakeTimers()
    let cache!: Cache

    setActiveSessionId('runtime-J')
    render(
      <Harness activeSessionId="runtime-J" onReady={value => (cache = value)} selectedStoredSessionId="stored-J1" />
    )

    act(() => {
      cache.updateSessionState(
        'runtime-J',
        state => ({ ...state, busy: true, messages: tail(), streamId: 'assistant-stream-1' }),
        'stored-J1'
      )
      vi.advanceTimersByTime(JOURNAL_PERSIST_THROTTLE_MS)
    })

    expect(readInFlightTurnJournal('stored-J1')?.runtimeSessionId).toBe('runtime-J')

    // Compression rotates the stored tip; the same live turn keeps streaming.
    act(() => {
      cache.updateSessionState(
        'runtime-J',
        state => ({ ...state, busy: true, messages: tail(), streamId: 'assistant-stream-1' }),
        'stored-J2'
      )
      vi.advanceTimersByTime(JOURNAL_PERSIST_THROTTLE_MS)
    })

    // The rotation link is what lets the write path drop the key it superseded,
    // so the pre-rotation tail is not stranded holding the same prompt.
    expect(readInFlightTurnJournal('stored-J1')).toBeNull()
    expect(readInFlightTurnJournal('stored-J2')?.runtimeSessionId).toBe('runtime-J')
  })
})
