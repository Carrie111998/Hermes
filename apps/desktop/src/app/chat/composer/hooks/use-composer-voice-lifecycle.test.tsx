import { act, cleanup, renderHook } from '@testing-library/react'
import { type PropsWithChildren } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ComposerScopeProvider, MAIN_COMPOSER_SCOPE } from '../scope'

import { useComposerVoice } from './use-composer-voice'

interface ConversationOptions {
  enabled: boolean
  onFatalError?: () => void
  onInterrupt?: () => Promise<void> | void
  onStopWord?: () => void
  onSubmit: (text: string) => Promise<void> | void
}

interface RecorderOptions {
  onTranscript: (text: string) => void
}

const mocks = vi.hoisted(() => ({
  conversationOptions: null as ConversationOptions | null,
  recorderOptions: null as RecorderOptions | null
}))

vi.mock('./use-auto-speak-replies', () => ({ useAutoSpeakReplies: vi.fn() }))
vi.mock('./use-voice-conversation', () => ({
  useVoiceConversation: vi.fn((options: ConversationOptions) => {
    mocks.conversationOptions = options

    return {
      end: vi.fn(),
      level: 0,
      muted: false,
      start: vi.fn(),
      status: 'idle',
      stopTurn: vi.fn(),
      toggleMute: vi.fn()
    }
  })
}))
vi.mock('./use-voice-recorder', () => ({
  useVoiceRecorder: vi.fn((options: RecorderOptions) => {
    mocks.recorderOptions = options

    return {
      dictate: vi.fn(),
      voiceActivityState: { elapsedSeconds: 0, level: 0, status: 'idle' },
      voiceStatus: 'idle'
    }
  })
}))

function Wrapper({ children }: PropsWithChildren) {
  return <ComposerScopeProvider value={MAIN_COMPOSER_SCOPE}>{children}</ComposerScopeProvider>
}

describe('useComposerVoice session-transition lifecycle', () => {
  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
    mocks.conversationOptions = null
    mocks.recorderOptions = null
  })

  it('drops A callbacks that finish after the composer has converged on B', async () => {
    const clearDraft = vi.fn()
    const focusInput = vi.fn()
    const insertText = vi.fn()
    const onInterrupt = vi.fn()
    const onSubmit = vi.fn(async () => true)
    const onTranscribeAudio = vi.fn(async () => 'transcript')

    const hook = renderHook(
      ({ disabled, submissionKey }: { disabled: boolean; submissionKey: string }) =>
        useComposerVoice({
          busy: false,
          clearDraft,
          disabled,
          focusInput,
          insertText,
          maxRecordingSeconds: 120,
          onInterrupt,
          onSubmit,
          onTranscribeAudio,
          sessionId: `runtime-${submissionKey}`,
          submissionKey,
          target: 'main'
        }),
      { initialProps: { disabled: false, submissionKey: 'session-a' }, wrapper: Wrapper }
    )

    act(() => hook.result.current.startConversation())
    expect(mocks.conversationOptions?.enabled).toBe(true)

    const conversationA = mocks.conversationOptions!
    const recorderA = mocks.recorderOptions!

    hook.rerender({ disabled: true, submissionKey: 'session-b' })
    expect(mocks.conversationOptions?.enabled).toBe(false)
    hook.rerender({ disabled: false, submissionKey: 'session-b' })

    act(() => hook.result.current.startConversation())
    expect(mocks.conversationOptions?.enabled).toBe(true)

    await act(async () => {
      recorderA.onTranscript('dictation from A')
      await conversationA.onSubmit('spoken turn from A')
      await conversationA.onInterrupt?.()
      conversationA.onFatalError?.()
      conversationA.onStopWord?.()
    })

    expect(insertText).not.toHaveBeenCalled()
    expect(clearDraft).not.toHaveBeenCalled()
    expect(onSubmit).not.toHaveBeenCalled()
    expect(onInterrupt).not.toHaveBeenCalled()
    expect(focusInput).not.toHaveBeenCalled()
    expect(mocks.conversationOptions?.enabled).toBe(true)
  })
})
