import { act, renderHook } from '@testing-library/react'
import { atom } from 'nanostores'
import type { ReadableAtom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ChatMessage } from '@/lib/chat-messages'
import { useAutoSpeakReplies } from '@/app/chat/composer/hooks/use-auto-speak-replies'
import { $autoSpeakReplies } from '@/store/voice-prefs'
import { $voicePlayback } from '@/store/voice-playback'

// ── mocked dependencies ──────────────────────────────────────────────────

const playSpeechText = vi.fn<[string, Record<string, unknown>], Promise<boolean>>()
const ownsAmbientCue = vi.fn<[string], Promise<boolean>>()
const notifyError = vi.fn()

vi.mock('@/lib/voice-playback', () => ({ playSpeechText }))
vi.mock('@/store/ambient', () => ({ ownsAmbientCue }))
vi.mock('@/store/notifications', () => ({ notifyError }))

const { useComposerScope } = vi.hoisted(() => ({ useComposerScope: vi.fn() }))
vi.mock('../scope', () => ({ useComposerScope }))

// ── helpers ──────────────────────────────────────────────────────────────

function pendingReply(value: { id: string; pending: boolean; text: string } | null) {
  return vi.fn(() => value)
}

interface HarnessOpts {
  conversationActive?: boolean
  sessionId?: string | null
}

function harness(markSpoken = vi.fn(), replyFn = pendingReply(null), opts: HarnessOpts = {}) {
  const messagesAtom = atom<ChatMessage[]>([])
  vi.mocked(useComposerScope).mockReturnValue({
    $messages: messagesAtom as ReadableAtom<ChatMessage[]>
  } as never)

  const result = renderHook(() =>
    useAutoSpeakReplies({
      conversationActive: opts.conversationActive ?? false,
      failureLabel: 'Read-aloud failed',
      markSpoken,
      pendingReply: replyFn,
      sessionId: opts.sessionId ?? 'session-1'
    })
  )

  return { markSpoken, messagesAtom, replyFn, result }
}

// ── setup / teardown ─────────────────────────────────────────────────────

beforeEach(() => {
  vi.useFakeTimers()
  ownsAmbientCue.mockResolvedValue(true)
  playSpeechText.mockResolvedValue(true)
  notifyError.mockReset()
  $autoSpeakReplies.set(true)
  $voicePlayback.set({
    audioElement: null,
    messageId: null,
    sequence: 0,
    source: null,
    status: 'idle'
  })
})

afterEach(() => {
  vi.useRealTimers()
  $autoSpeakReplies.set(false)
})

// ── tests ────────────────────────────────────────────────────────────────

describe('useAutoSpeakReplies retry timer', () => {
  it('speaks exactly once when a pending reply settles', async () => {
    const markSpoken = vi.fn()
    const reply = { id: 'msg-1', pending: false, text: 'Hello' }
    // Two subscription calls fire synchronously during effect setup
    // ($messages.subscribe + $voicePlayback.listen), then one timer callback.
    const replyFn = vi.fn()
      .mockReturnValueOnce({ ...reply, pending: true })
      .mockReturnValueOnce({ ...reply, pending: true })
      .mockReturnValue(reply)

    harness(markSpoken, replyFn)

    // Both subscriptions saw pending → one timer survives at 100ms.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(markSpoken).not.toHaveBeenCalled()
    expect(playSpeechText).not.toHaveBeenCalled()

    // Timer fires: retry, now reply is settled → speaks
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100)
    })
    expect(markSpoken).toHaveBeenCalledTimes(1)
    expect(playSpeechText).toHaveBeenCalledTimes(1)
    expect(playSpeechText).toHaveBeenCalledWith('Hello', { messageId: 'msg-1', source: 'read-aloud' })
  })

  it('does not speak when auto-speak is disabled before the retry fires', async () => {
    const markSpoken = vi.fn()
    const reply = { id: 'msg-1', pending: true, text: 'Hello' }
    const replyFn = pendingReply(reply)

    harness(markSpoken, replyFn)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })

    // Disable before the retry fires
    act(() => {
      $autoSpeakReplies.set(false)
    })

    // Advance past the timer
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200)
    })

    expect(markSpoken).not.toHaveBeenCalled()
    expect(playSpeechText).not.toHaveBeenCalled()
  })

  it('stops retrying after MAX_RETRIES (3) when reply stays pending', async () => {
    const markSpoken = vi.fn()
    const reply = { id: 'msg-1', pending: true, text: 'Hello' }
    const replyFn = pendingReply(reply)
    // Setup: subscribe + listen each fire speakLatest → count reaches 2,
    // one timer survives. Each timer fire increments count (2→3→stop).
    // After 3 increments total, speakLatest bails without scheduling.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(400)
    })

    // No further timers fire — speech never triggered.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500)
    })

    expect(markSpoken).not.toHaveBeenCalled()
    expect(playSpeechText).not.toHaveBeenCalled()
  })

  it('does not fire the timer callback after unmount', async () => {
    const markSpoken = vi.fn()
    const reply = { id: 'msg-1', pending: true, text: 'Hello' }
    const replyFn = pendingReply(reply)

    const { result } = harness(markSpoken, replyFn)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })

    // Unmount before the timer fires
    act(() => {
      result.unmount()
    })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(200)
    })

    expect(markSpoken).not.toHaveBeenCalled()
    expect(playSpeechText).not.toHaveBeenCalled()
  })

  it('resets the retry counter after a successful settle-and-speak', async () => {
    const markSpoken = vi.fn()

    // First sweep: two subscription calls (subscribe + listen) see pending,
    // then timer callback sees settled.
    const replyFn1 = vi.fn()
      .mockReturnValueOnce({ id: 'msg-1', pending: true, text: 'First' })
      .mockReturnValueOnce({ id: 'msg-1', pending: true, text: 'First' })
      .mockReturnValue({ id: 'msg-1', pending: false, text: 'First' })

    const { messagesAtom, replyFn } = harness(markSpoken, replyFn1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })

    // Timer fires → reply settled → speaks (counter reset in settled path)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100)
    })
    expect(markSpoken).toHaveBeenCalledTimes(1)
    expect(playSpeechText).toHaveBeenCalledWith('First', expect.anything())

    // Second stream: $messages.set triggers ONE subscription call.
    const secondReplyFn = vi.fn()
      .mockReturnValueOnce({ id: 'msg-2', pending: true, text: 'Second' })
      .mockReturnValue({ id: 'msg-2', pending: false, text: 'Second' })

    replyFn.mockImplementation(secondReplyFn)
    markSpoken.mockClear()
    playSpeechText.mockClear()

    // Simulate a new message arriving
    act(() => {
      messagesAtom.set([{ role: 'assistant' } as ChatMessage])
    })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })

    // Retry → settles → speaks again (counter was reset, so retry worked)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100)
    })

    expect(markSpoken).toHaveBeenCalledTimes(1)
    expect(playSpeechText).toHaveBeenCalledWith('Second', expect.anything())
  })
})
