import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { MicRecording } from './use-mic-recorder'

const handle = {
  cancel: vi.fn(),
  start: vi.fn(async () => undefined),
  stop: vi.fn<() => Promise<MicRecording | null>>(async () => ({
    audio: new Blob(['audio']),
    durationMs: 100,
    heardSpeech: true
  }))
}

const notify = vi.fn()

vi.mock('./use-mic-recorder', () => ({
  useMicRecorder: () => ({ handle, level: 0, recording: false })
}))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      notifications: {
        voice: {
          noSpeechDetected: 'No speech detected',
          recordingFailed: 'Recording failed',
          transcriptionFailed: 'Transcription failed',
          transcriptionUnavailable: 'Transcription unavailable',
          tryRecordingAgain: 'Try again',
          unavailable: 'Unavailable'
        }
      }
    }
  })
}))

vi.mock('@/store/notifications', () => ({ notify, notifyError: vi.fn() }))

import { useVoiceRecorder } from './use-voice-recorder'

afterEach(() => {
  handle.cancel.mockReset()
  handle.start.mockClear()
  handle.stop.mockClear()
  notify.mockClear()
})

describe('useVoiceRecorder dictation', () => {
  it('inserts a completed transcript into the draft callback without submitting', async () => {
    const onTranscript = vi.fn()

    const onSubmit = vi.fn()

    const { result } = renderHook(() =>
      useVoiceRecorder({
        focusInput: vi.fn(),
        maxRecordingSeconds: 30,
        onTranscript,
        onTranscribeAudio: vi.fn(async () => 'dictated words')
      })
    )

    await act(async () => {
      await result.current.start()
      await result.current.stop()
    })

    expect(onTranscript).toHaveBeenCalledWith('dictated words')
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('warns and leaves the draft untouched for an empty transcript', async () => {
    const onTranscript = vi.fn()

    const { result } = renderHook(() =>
      useVoiceRecorder({
        focusInput: vi.fn(),
        maxRecordingSeconds: 30,
        onTranscript,
        onTranscribeAudio: vi.fn(async () => '  ')
      })
    )

    await act(async () => {
      await result.current.start()
      await result.current.stop()
    })

    expect(onTranscript).not.toHaveBeenCalled()
    expect(notify).toHaveBeenCalledWith(expect.objectContaining({ kind: 'warning', title: 'No speech detected' }))
  })

  it('discards a transcription that completes after cancellation', async () => {
    const onTranscript = vi.fn()
    let completeTranscription: (text: string) => void = () => undefined

    const transcription = new Promise<string>(resolve => {
      completeTranscription = resolve
    })

    const onTranscribeAudio = vi.fn(() => transcription)

    const { result } = renderHook(() =>
      useVoiceRecorder({
        focusInput: vi.fn(),
        maxRecordingSeconds: 30,
        onTranscript,
        onTranscribeAudio
      })
    )

    await act(async () => {
      await result.current.start()
    })

    let stopping!: Promise<void>
    await act(async () => {
      stopping = result.current.stop()
      await Promise.resolve()
    })

    expect(onTranscribeAudio).toHaveBeenCalledOnce()

    act(() => {
      result.current.cancel()
    })

    await act(async () => {
      completeTranscription('discard this')
      await stopping!
    })

    expect(onTranscript).not.toHaveBeenCalled()
  })
})
