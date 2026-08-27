import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { type PropsWithChildren } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { $gateway } from '@/store/gateway'

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
  focusInput: () => void
  onTranscript: (text: string) => void
}

const mocks = vi.hoisted(() => ({
  conversationOptions: null as ConversationOptions | null,
  recorderCancel: vi.fn(),
  recorderOptions: null as RecorderOptions | null,
  resumeWakeAfterVoice: vi.fn(async () => undefined)
}))

vi.mock('@/store/wake-word', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  resumeWakeAfterVoice: mocks.resumeWakeAfterVoice
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
      cancel: mocks.recorderCancel,
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
    mocks.recorderCancel.mockReset()
    mocks.recorderOptions = null
    mocks.resumeWakeAfterVoice.mockReset()
    $gateway.set(null)
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
    expect(mocks.recorderCancel).toHaveBeenCalledOnce()
    hook.rerender({ disabled: false, submissionKey: 'session-b' })
    expect(mocks.conversationOptions?.enabled).toBe(false)

    act(() => hook.result.current.startConversation())
    expect(mocks.conversationOptions?.enabled).toBe(true)

    await act(async () => {
      recorderA.onTranscript('dictation from A')
      recorderA.focusInput()
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

  it('does not let a stale wake resume cross a rapid conversation restart', async () => {
    const pauseResolvers: (() => void)[] = []

    const request = vi.fn(
      () =>
        new Promise<void>(resolve => {
          pauseResolvers.push(resolve)
        })
    )

    $gateway.set({ request } as never)

    const hook = renderHook(
      () =>
        useComposerVoice({
          busy: false,
          clearDraft: vi.fn(),
          disabled: false,
          focusInput: vi.fn(),
          insertText: vi.fn(),
          maxRecordingSeconds: 120,
          onSubmit: vi.fn(async () => true),
          onTranscribeAudio: vi.fn(async () => 'transcript'),
          sessionId: 'runtime-a',
          submissionKey: 'session-a',
          target: 'main'
        }),
      { wrapper: Wrapper }
    )

    act(() => hook.result.current.startConversation())
    await waitFor(() => expect(request).toHaveBeenCalledTimes(1))
    mocks.resumeWakeAfterVoice.mockClear()
    act(() => hook.result.current.endConversation())
    act(() => hook.result.current.startConversation())

    await act(async () => pauseResolvers[0]?.())
    await waitFor(() => expect(request).toHaveBeenCalledTimes(2))

    expect(mocks.resumeWakeAfterVoice).not.toHaveBeenCalled()
  })
})
