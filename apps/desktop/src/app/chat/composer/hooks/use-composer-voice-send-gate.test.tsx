import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { requestVoiceToggle } from '../focus'
import { ComposerScopeProvider, MAIN_COMPOSER_SCOPE } from '../scope'

import { useComposerVoice } from './use-composer-voice'

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
  useVoiceConversation: () => ({
    end: vi.fn(async () => undefined),
    level: 0,
    muted: false,
    start: vi.fn(),
    status: 'idle',
    stopTurn: vi.fn(),
    toggleMute: vi.fn()
  })
}))

vi.mock('./use-auto-speak-replies', () => ({
  useAutoSpeakReplies: () => undefined
}))

vi.mock('@/lib/wake-indicator', () => ({
  clearWakeIndicator: vi.fn(),
  syncWakeIndicatorWithVoice: vi.fn()
}))

function renderVoice(disabled: boolean) {
  return renderHook(
    () =>
      useComposerVoice({
        busy: false,
        clearDraft: vi.fn(),
        disabled,
        focusInput: vi.fn(),
        insertText: vi.fn(),
        maxRecordingSeconds: 120,
        onSubmit: vi.fn(async () => true),
        onTranscribeAudio: vi.fn(async () => ''),
        sessionId: 'rt-voice',
        target: 'main'
      }),
    {
      wrapper: ({ children }) => <ComposerScopeProvider value={MAIN_COMPOSER_SCOPE}>{children}</ComposerScopeProvider>
    }
  )
}

describe('useComposerVoice workspace send gate', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
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
})
