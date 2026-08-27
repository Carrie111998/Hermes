import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { assistantTextPart, type ChatMessage, chatMessageText } from '@/lib/chat-messages'
import { clearSpokenRepliesForTests, markAssistantIdSpoken, resolveSpokenReply } from '@/lib/spoken-reply'
import { playSpeechText } from '@/lib/voice-playback'
import { $voicePlayback, setVoicePlaybackState } from '@/store/voice-playback'
import { $autoSpeakReplies } from '@/store/voice-prefs'

import { ComposerScopeProvider, MAIN_COMPOSER_SCOPE } from '../scope'

import { useAutoSpeakReplies } from './use-auto-speak-replies'

const ownsAmbientCueMock = vi.hoisted(() => vi.fn())

vi.mock('@/lib/voice-playback', () => ({
  playSpeechText: vi.fn()
}))

vi.mock('@/store/ambient', () => ({
  ownsAmbientCue: ownsAmbientCueMock
}))

const SESSION_ID = 'session-under-test'
const IDLE_STATE = { audioElement: null, messageId: null, sequence: 0, source: null, status: 'idle' as const }

function assistantMessage(id: string, text: string): ChatMessage {
  return { id, parts: [assistantTextPart(text)], role: 'assistant' }
}

// #93515 — Edge TTS has no chunked-PCM API, so the WS attempt in
// playSpeechText's fallback ladder settles 'fallback' before any audio plays
// and the client retries over the POST endpoint. While that POST round-trip
// is in flight, the backend can rewrite the just-completed reply's renderer
// id (`assistant-stream-*`) to its durable id. The issue claims
// `resolveSpokenReply()` fails to follow that rewrite and the reply gets
// spoken a second time once `$voicePlayback` goes idle.
describe('useAutoSpeakReplies — Edge TTS fallback chain (#93515)', () => {
  afterEach(() => {
    cleanup()
    clearSpokenRepliesForTests()
    $autoSpeakReplies.set(false)
    setVoicePlaybackState({ ...IDLE_STATE })
    vi.clearAllMocks()
  })

  it('does not re-speak the reply once playback goes idle after an id rewrite mid-fallback', async () => {
    $autoSpeakReplies.set(true)
    ownsAmbientCueMock.mockResolvedValue(true)

    const $messages = atom<ChatMessage[]>([])

    // The exact pendingReply/markSpoken contract use-composer-voice.ts wires
    // up for this hook, backed by the real ordinal-anchored dedupe.
    const pendingReply = () => {
      const messages = $messages.get()
      const last = messages.findLast(m => m.role === 'assistant' && !m.hidden)
      const spoken = resolveSpokenReply(SESSION_ID, messages)

      if (!last || last.id === spoken?.id) {
        return null
      }

      return { id: last.id, pending: Boolean(last.pending), text: chatMessageText(last) }
    }

    const markSpoken = () => {
      const messages = $messages.get()
      const last = messages.findLast(m => m.role === 'assistant' && !m.hidden)

      if (last) {
        markAssistantIdSpoken(SESSION_ID, messages, last.id)
      }
    }

    let settleFallback: (() => void) | null = null

    vi.mocked(playSpeechText).mockImplementation(async () => {
      setVoicePlaybackState({
        audioElement: null,
        messageId: 'assistant-stream-1',
        sequence: 0,
        source: 'read-aloud',
        status: 'preparing'
      })

      // Holds mid-ladder — the WS-fallback-then-POST round trip the issue
      // describes — until the test rewrites the message id underneath it.
      await new Promise<void>(resolve => {
        settleFallback = resolve
      })

      $messages.set([assistantMessage('durable-42', 'hello there')])

      setVoicePlaybackState({
        audioElement: null,
        messageId: 'durable-42',
        sequence: 0,
        source: 'read-aloud',
        status: 'idle'
      })

      return true
    })

    renderHook(
      () =>
        useAutoSpeakReplies({
          conversationActive: false,
          failureLabel: 'read-aloud failed',
          markSpoken,
          pendingReply,
          sessionId: SESSION_ID
        }),
      {
        wrapper: ({ children }) => (
          <ComposerScopeProvider value={{ ...MAIN_COMPOSER_SCOPE, $messages }}>{children}</ComposerScopeProvider>
        )
      }
    )

    act(() => {
      $messages.set([assistantMessage('assistant-stream-1', 'hello there')])
    })

    await waitFor(() => expect(playSpeechText).toHaveBeenCalledTimes(1))

    await act(async () => {
      settleFallback?.()
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect($voicePlayback.get().status).toBe('idle')
    expect(playSpeechText).toHaveBeenCalledTimes(1)
  })
})

describe('useAutoSpeakReplies — async ownership', () => {
  afterEach(() => {
    cleanup()
    $autoSpeakReplies.set(false)
    setVoicePlaybackState({ ...IDLE_STATE })
    vi.clearAllMocks()
  })

  it('does not start stale speech after its composer scope is replaced', async () => {
    $autoSpeakReplies.set(true)
    const $messages = atom<ChatMessage[]>([])
    let resolveOwnership: ((owns: boolean) => void) | undefined

    ownsAmbientCueMock.mockReturnValueOnce(
      new Promise<boolean>(resolve => {
        resolveOwnership = resolve
      })
    )
    vi.mocked(playSpeechText).mockImplementation(async (_text, options) => {
      setVoicePlaybackState({
        audioElement: null,
        messageId: options.messageId ?? null,
        sequence: 12,
        source: options.source,
        status: 'preparing'
      })

      return true
    })

    interface HookProps {
      pendingReply: () => null | { id: string; pending: boolean; text: string }
      sessionId: string
    }

    const hook = renderHook(
      ({ pendingReply, sessionId }: HookProps) =>
        useAutoSpeakReplies({
          conversationActive: false,
          failureLabel: 'read-aloud failed',
          markSpoken: vi.fn(),
          pendingReply,
          sessionId
        }),
      {
        initialProps: {
          pendingReply: () => ({ id: 'old-reply', pending: false, text: 'old scope' }),
          sessionId: 'old-session'
        } as HookProps,
        wrapper: ({ children }) => (
          <ComposerScopeProvider value={{ ...MAIN_COMPOSER_SCOPE, $messages }}>{children}</ComposerScopeProvider>
        )
      }
    )

    await waitFor(() => expect(ownsAmbientCueMock).toHaveBeenCalledWith('speak:old-reply'))

    act(() => {
      setVoicePlaybackState({
        audioElement: null,
        messageId: 'new-session-reply',
        sequence: 11,
        source: 'read-aloud',
        status: 'preparing'
      })
      hook.rerender({ pendingReply: () => null, sessionId: 'new-session' })
    })

    await act(async () => {
      resolveOwnership?.(true)
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(playSpeechText).not.toHaveBeenCalled()
    expect($voicePlayback.get()).toMatchObject({ messageId: 'new-session-reply', sequence: 11, status: 'preparing' })
  })

  it('does not speak an older claim after a newer reply loses ownership', async () => {
    $autoSpeakReplies.set(true)
    const $messages = atom<ChatMessage[]>([])
    const ownershipResolvers = new Map<string, (owns: boolean) => void>()
    let currentReply: null | { id: string; pending: boolean; text: string } = null

    ownsAmbientCueMock.mockImplementation(
      (key: string) =>
        new Promise<boolean>(resolve => {
          ownershipResolvers.set(key, resolve)
        })
    )
    vi.mocked(playSpeechText).mockResolvedValue(true)

    renderHook(
      () =>
        useAutoSpeakReplies({
          conversationActive: false,
          failureLabel: 'read-aloud failed',
          markSpoken: vi.fn(),
          pendingReply: () => currentReply,
          sessionId: SESSION_ID
        }),
      {
        wrapper: ({ children }) => (
          <ComposerScopeProvider value={{ ...MAIN_COMPOSER_SCOPE, $messages }}>{children}</ComposerScopeProvider>
        )
      }
    )

    act(() => {
      currentReply = { id: 'older', pending: false, text: 'older reply' }
      $messages.set([assistantMessage('older', 'older reply')])
    })
    await waitFor(() => expect(ownsAmbientCueMock).toHaveBeenCalledWith('speak:older'))

    act(() => {
      currentReply = { id: 'newer', pending: false, text: 'newer reply' }
      $messages.set([assistantMessage('older', 'older reply'), assistantMessage('newer', 'newer reply')])
    })
    await waitFor(() => expect(ownsAmbientCueMock).toHaveBeenCalledWith('speak:newer'))

    await act(async () => {
      ownershipResolvers.get('speak:newer')?.(false)
      await Promise.resolve()
      ownershipResolvers.get('speak:older')?.(true)
      await Promise.resolve()
    })

    expect(playSpeechText).not.toHaveBeenCalled()
  })
})
