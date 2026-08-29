import { act, cleanup, render, waitFor } from '@testing-library/react'
import type { MutableRefObject } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import { $composerDraft } from '@/store/composer'
import {
  $parkedQueueSessions,
  $queuedPromptsBySession,
  enqueueQueuedPrompt,
  getQueuedPrompts,
  parkQueuedPrompts,
  removeQueuedPrompt,
  resetQueueDrainState
} from '@/store/composer-queue'
import { $notifications } from '@/store/notifications'
import { $sessions, setSessions } from '@/store/session'
import { clearAllSessionStates, publishSessionState } from '@/store/session-states'
import type { SessionInfo } from '@/types/hermes'

import { useBackgroundQueueDrain } from './use-background-queue-drain'
import type { SubmitTextOptions } from './use-prompt-actions/utils'

const lineageSession = (over: Partial<SessionInfo>): SessionInfo =>
  ({
    archived: false,
    cwd: null,
    ended_at: null,
    id: 'live',
    input_tokens: 0,
    is_active: false,
    last_active: 0,
    message_count: 0,
    model: null,
    output_tokens: 0,
    preview: null,
    source: null,
    started_at: 0,
    title: null,
    tool_call_count: 0,
    ...over
  }) as SessionInfo

function Harness({
  enabled = true,
  onOpenSession,
  runtimeMap,
  selectedStoredSessionId = 'stored-session-b',
  submitText
}: {
  enabled?: boolean
  onOpenSession?: (storedSessionId: string) => void
  runtimeMap: MutableRefObject<Map<string, string>>
  selectedStoredSessionId?: string | null
  submitText: (text: string, options?: SubmitTextOptions) => Promise<boolean> | boolean
}) {
  useBackgroundQueueDrain({
    enabled,
    onOpenSession,
    runtimeIdByStoredSessionIdRef: runtimeMap,
    selectedStoredSessionId,
    submitText
  })

  return null
}

describe('useBackgroundQueueDrain', () => {
  // Each retry needs its own act(): the timer fires, React re-renders, and only
  // then does the effect start the next attempt — one advance cannot chase a
  // chain that hops through the scheduler between every link.
  const exhaustRetries = async () => {
    for (const ms of [1_000, 4_000, 10_000]) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(ms)
      })
    }
  }

  beforeEach(() => {
    vi.useRealTimers()
    clearAllSessionStates()
    // Failure counts and raised alarms are module state now — deliberately, so
    // a remount cannot buy an unsendable entry four more attempts — which means
    // they outlive a test case too unless cleared here.
    resetQueueDrainState()
    $notifications.set([])
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    vi.useRealTimers()
    $queuedPromptsBySession.set({})
    $parkedQueueSessions.set({})
    $sessions.set([])
    clearAllSessionStates()
    resetQueueDrainState()
    $notifications.set([])
  })

  it('drains an idle queued prompt for a non-selected background session', async () => {
    const runtimeMap = { current: new Map([['stored-session-a', 'rt-session-a']]) }
    const submitText = vi.fn(async () => true)

    enqueueQueuedPrompt('stored-session-a', { text: 'continue in the background', attachments: [] })
    clearAllSessionStates()

    render(<Harness runtimeMap={runtimeMap} submitText={submitText} />)

    await waitFor(() => {
      expect(submitText).toHaveBeenCalledWith('continue in the background', {
        attachments: [],
        fromQueue: true,
        sessionId: 'rt-session-a',
        storedSessionId: 'stored-session-a'
      })
    })

    await waitFor(() => expect(getQueuedPrompts('stored-session-a')).toHaveLength(0))
  })

  it('leaves the selected session queue to the mounted ChatBar drainer', async () => {
    const runtimeMap = { current: new Map([['stored-session-a', 'rt-session-a']]) }
    const submitText = vi.fn(async () => true)

    enqueueQueuedPrompt('stored-session-a', { text: 'visible queue entry', attachments: [] })
    clearAllSessionStates()

    render(<Harness runtimeMap={runtimeMap} selectedStoredSessionId="stored-session-a" submitText={submitText} />)

    await new Promise(resolve => window.setTimeout(resolve, 0))

    expect(submitText).not.toHaveBeenCalled()
    expect(getQueuedPrompts('stored-session-a')).toHaveLength(1)
  })

  it('does not drain a background session that is still marked working', async () => {
    const runtimeMap = { current: new Map([['stored-session-a', 'rt-session-a']]) }
    const submitText = vi.fn(async () => true)

    enqueueQueuedPrompt('stored-session-a', { text: 'wait for current turn', attachments: [] })
    // Mark the session as working (busy) so the drain should skip it
    publishSessionState('rt-session-a', { ...createClientSessionState('stored-session-a'), busy: true })

    render(<Harness runtimeMap={runtimeMap} submitText={submitText} />)

    await new Promise(resolve => window.setTimeout(resolve, 0))

    expect(submitText).not.toHaveBeenCalled()
    expect(getQueuedPrompts('stored-session-a')).toHaveLength(1)
  })

  it('treats a tip working id as busy for a root queue key via lineage', async () => {
    // Queue keys use the lineage root (resolveComposerSessionKey) while
    // $workingSessionIds may hold the compression tip — strict equality misses.
    const runtimeMap = { current: new Map([['root-a', 'rt-tip-a']]) }
    const submitText = vi.fn(async () => true)

    setSessions([lineageSession({ id: 'tip-a', _lineage_root_id: 'root-a' })])
    enqueueQueuedPrompt('root-a', { text: 'wait for tip turn', attachments: [] })
    publishSessionState('rt-tip-a', { ...createClientSessionState('tip-a'), busy: true })

    render(<Harness runtimeMap={runtimeMap} submitText={submitText} />)

    await new Promise(resolve => window.setTimeout(resolve, 0))

    expect(submitText).not.toHaveBeenCalled()
    expect(getQueuedPrompts('root-a')).toHaveLength(1)
  })

  it('leaves a root queue to ChatBar when the selected id is the compression tip', async () => {
    const runtimeMap = { current: new Map([['root-a', 'rt-tip-a']]) }
    const submitText = vi.fn(async () => true)

    setSessions([lineageSession({ id: 'tip-a', _lineage_root_id: 'root-a' })])
    enqueueQueuedPrompt('root-a', { text: 'visible after tip select', attachments: [] })
    clearAllSessionStates()

    render(<Harness runtimeMap={runtimeMap} selectedStoredSessionId="tip-a" submitText={submitText} />)

    await new Promise(resolve => window.setTimeout(resolve, 0))

    expect(submitText).not.toHaveBeenCalled()
    expect(getQueuedPrompts('root-a')).toHaveLength(1)
  })

  it('does not drain a parked background session, even when idle', async () => {
    // A Stop in a tile parks that session's queue; when the user then focuses
    // another chat, THIS drainer takes over the tile's queue — it must honor
    // the park just like the mounted ChatBar drainer does.
    const runtimeMap = { current: new Map([['stored-session-a', 'rt-session-a']]) }
    const submitText = vi.fn(async () => true)

    enqueueQueuedPrompt('stored-session-a', { text: 'halted by stop', attachments: [] })
    parkQueuedPrompts('stored-session-a')
    clearAllSessionStates()

    render(<Harness runtimeMap={runtimeMap} submitText={submitText} />)

    await new Promise(resolve => window.setTimeout(resolve, 0))

    expect(submitText).not.toHaveBeenCalled()
    expect(getQueuedPrompts('stored-session-a')).toHaveLength(1)
  })

  it('passes a null runtime id so submitText can resume stale background sessions by stored id', async () => {
    const runtimeMap = { current: new Map<string, string>() }
    const submitText = vi.fn(async () => true)

    enqueueQueuedPrompt('stored-session-a', { text: 'resume then send', attachments: [] })

    render(<Harness runtimeMap={runtimeMap} submitText={submitText} />)

    await waitFor(() => {
      expect(submitText).toHaveBeenCalledWith('resume then send', {
        attachments: [],
        fromQueue: true,
        sessionId: null,
        storedSessionId: 'stored-session-a'
      })
    })
  })

  it('retries a rejected background drain without waiting for another queue or busy-state change', async () => {
    vi.useFakeTimers()

    const runtimeMap = { current: new Map([['stored-session-a', 'rt-session-a']]) }
    const submitText = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true)

    enqueueQueuedPrompt('stored-session-a', { text: 'retry me', attachments: [] })

    render(<Harness runtimeMap={runtimeMap} submitText={submitText} />)

    await act(async () => {
      await Promise.resolve()
    })

    expect(submitText).toHaveBeenCalledTimes(1)
    expect(getQueuedPrompts('stored-session-a')).toHaveLength(1)

    // 1s, not the old flat 750ms — the retries back off now.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000)
      await Promise.resolve()
    })

    expect(submitText).toHaveBeenCalledTimes(2)
    expect(getQueuedPrompts('stored-session-a')).toHaveLength(0)
  })

  it('backs off between retries, so a gateway bounce outlives the budget instead of alarming', async () => {
    vi.useFakeTimers()

    const runtimeMap = { current: new Map([['stored-session-a', 'rt-session-a']]) }
    const submitText = vi.fn(async () => false)

    enqueueQueuedPrompt('stored-session-a', { text: 'keeps failing', attachments: [] })

    render(<Harness runtimeMap={runtimeMap} submitText={submitText} />)

    await act(async () => {
      await Promise.resolve()
    })
    expect(submitText).toHaveBeenCalledTimes(1)

    // The second gap is longer than the first: at a flat 750ms the whole budget
    // was spent in ~3s, which is shorter than a gateway restart.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000)
    })
    expect(submitText).toHaveBeenCalledTimes(2)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000)
    })
    expect(submitText).toHaveBeenCalledTimes(2)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000)
    })
    expect(submitText).toHaveBeenCalledTimes(3)
  })

  it('names the chat and offers a way into it when a background queue gives up', async () => {
    vi.useFakeTimers()

    const runtimeMap = { current: new Map([['stored-session-a', 'rt-session-a']]) }
    const submitText = vi.fn(async () => false)
    const onOpenSession = vi.fn()

    setSessions([lineageSession({ id: 'stored-session-a', title: 'Sdeira Group site' })])
    enqueueQueuedPrompt('stored-session-a', { text: 'never sends', attachments: [] })

    render(<Harness onOpenSession={onOpenSession} runtimeMap={runtimeMap} submitText={submitText} />)

    await exhaustRetries()

    const toast = $notifications.get().find(n => n.id === 'composer-queue-stuck-stored-session-a')

    expect(toast?.message).toContain('Sdeira Group site')
    expect(toast?.action).toBeTruthy()

    toast?.action?.onClick()
    expect(onOpenSession).toHaveBeenCalledWith('stored-session-a')
  })

  it('offers the words back when the queue belongs to a chat that no longer exists', async () => {
    vi.useFakeTimers()

    const runtimeMap = { current: new Map<string, string>() }
    const submitText = vi.fn(async () => false)

    // A live sessions list that simply does not contain the queue's key: the
    // signature of a fresh chat whose runtime id died with the process.
    setSessions([lineageSession({ id: 'stored-session-b', title: 'Something else' })])
    enqueueQueuedPrompt('dead-runtime-id', { text: 'two days of silence', attachments: [] })

    render(<Harness runtimeMap={runtimeMap} submitText={submitText} />)

    await exhaustRetries()

    const toast = $notifications.get().find(n => n.id === 'composer-queue-stuck-dead-runtime-id')

    // Not an error telling them to retry — retrying is exactly what can never work.
    expect(toast?.kind).toBe('warning')
    expect(toast?.action).toBeTruthy()

    toast?.action?.onClick()
    expect(getQueuedPrompts('dead-runtime-id')).toHaveLength(0)
    expect($composerDraft.get()).toContain('two days of silence')
  })

  it('takes the alarm down once the entry is gone', async () => {
    vi.useFakeTimers()

    const runtimeMap = { current: new Map([['stored-session-a', 'rt-session-a']]) }
    const submitText = vi.fn(async () => false)

    setSessions([lineageSession({ id: 'stored-session-a', title: 'Still here' })])
    const entry = enqueueQueuedPrompt('stored-session-a', { text: 'stuck', attachments: [] })!

    render(<Harness runtimeMap={runtimeMap} submitText={submitText} />)

    await exhaustRetries()

    expect($notifications.get().some(n => n.id === 'composer-queue-stuck-stored-session-a')).toBe(true)

    // The user deletes it from the panel. The alarm described that entry; with
    // the entry gone it describes nothing, and used to stay on screen forever.
    act(() => {
      removeQueuedPrompt('stored-session-a', entry.id)
    })

    expect($notifications.get().some(n => n.id === 'composer-queue-stuck-stored-session-a')).toBe(false)
  })
})
