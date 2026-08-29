import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { requestVoiceToggle } from '../focus'
import { ComposerScopeProvider, MAIN_COMPOSER_SCOPE } from '../scope'

import { useComposerVoice } from './use-composer-voice'

const voiceHarness = vi.hoisted(() => ({
  end: vi.fn(async () => undefined),
  submit: null as null | ((text: string) => Promise<void>)
}))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      assistant: { thread: { readAloudFailed: 'read aloud failed' } },
      notifications: {
        voice: {
          configureSpeechToText: 'configure STT',
          couldNotStartSession: 'could not start',
          microphoneFailed: 'mic failed',
          playbackFailed: 'playback failed',
          sayStopToEnd: (phrase: string) => `say ${phrase}`,
          transcriptionFailed: 'transcription failed',
          unavailable: 'unavailable'
        }
      },
      settings: { config: { autosaveFailed: 'autosave failed' } }
    }
  })
}))

vi.mock('./use-voice-recorder', () => ({
  useVoiceRecorder: () => ({
    dictate: vi.fn(),
    voiceActivityState: null,
    voiceStatus: 'idle'
  })
}))

vi.mock('./use-voice-conversation', () => ({
  useVoiceConversation: ({ onSubmit }: { onSubmit: (text: string) => Promise<void> }) => {
    voiceHarness.submit = onSubmit

    return {
      end: voiceHarness.end,
      level: 0,
      muted: false,
      start: vi.fn(),
      status: 'idle',
      stopTurn: vi.fn(),
      toggleMute: vi.fn()
    }
  }
}))

vi.mock('./use-auto-speak-replies', () => ({
  useAutoSpeakReplies: () => undefined
}))

vi.mock('@/lib/wake-indicator', () => ({
  clearWakeIndicator: vi.fn(),
  syncWakeIndicatorWithVoice: vi.fn()
}))

function renderVoice(disabled: boolean) {
  const clearDraft = vi.fn()
  const onSubmit = vi.fn(async () => true)

  const hook = renderHook(
    ({ blocked }) =>
      useComposerVoice({
        busy: false,
        clearDraft,
        disabled: blocked,
        focusInput: vi.fn(),
        insertText: vi.fn(),
        maxRecordingSeconds: 120,
        onSubmit,
        onTranscribeAudio: vi.fn(async () => ''),
        sessionId: 'rt-voice',
        target: 'main'
      }),
    {
      initialProps: { blocked: disabled },
      wrapper: ({ children }) => <ComposerScopeProvider value={MAIN_COMPOSER_SCOPE}>{children}</ComposerScopeProvider>
    }
  )

  return { ...hook, clearDraft, onSubmit }
}

describe('useComposerVoice workspace send gate', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    voiceHarness.end.mockClear()
    voiceHarness.submit = null
  })

  it('does not start or toggle voice while send is blocked', () => {
    const { result } = renderVoice(true)

    act(() => {
      result.current.startConversation()
      requestVoiceToggle('main')
    })

    expect(result.current.voiceConversationActive).toBe(false)
  })

  it('starts a voice conversation when send is allowed', () => {
    const { result } = renderVoice(false)

    act(() => {
      result.current.startConversation()
    })

    expect(result.current.voiceConversationActive).toBe(true)
  })

  it('ends an active voice conversation when the workspace send barrier closes', async () => {
    const { rerender, result } = renderVoice(false)

    act(() => {
      result.current.startConversation()
    })

    rerender({ blocked: true })

    await act(async () => {
      await Promise.resolve()
    })

    expect(result.current.voiceConversationActive).toBe(false)
    expect(voiceHarness.end).toHaveBeenCalledTimes(1)
  })

  it('does not clear or submit a voice transcript while send is blocked', async () => {
    const { clearDraft, onSubmit } = renderVoice(true)

    await act(async () => {
      await voiceHarness.submit?.('stay on the original workspace')
    })

    expect(onSubmit).not.toHaveBeenCalled()
    expect(clearDraft).not.toHaveBeenCalled()
  })
})
