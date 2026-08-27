import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mic = vi.hoisted(() => ({
  cancel: vi.fn(),
  recording: false,
  start: vi.fn<() => Promise<void>>(),
  stop: vi.fn<() => Promise<{ audio: Blob; durationMs: number; heardSpeech: boolean } | null>>()
}))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      notifications: {
        voice: {
          noSpeechDetected: 'No speech',
          recordingFailed: 'Recording failed',
          transcriptionFailed: 'Transcription failed',
          transcriptionUnavailable: 'Unavailable',
          tryRecordingAgain: 'Try again',
          unavailable: 'Unavailable'
        }
      }
    }
  })
}))

vi.mock('./use-mic-recorder', () => ({
  useMicRecorder: () => ({
    handle: { cancel: mic.cancel, start: mic.start, stop: mic.stop },
    level: 0,
    recording: mic.recording
  })
}))

import { useVoiceRecorder } from './use-voice-recorder'

describe('useVoiceRecorder lifecycle', () => {
  beforeEach(() => {
    mic.cancel.mockReset()
    mic.start.mockReset()
    mic.stop.mockReset()
    mic.recording = false
  })

  it('serializes rapid Dictate starts while microphone acquisition is pending', async () => {
    let resolveStart: (() => void) | undefined
    mic.start.mockImplementation(
      () =>
        new Promise<void>(resolve => {
          resolveStart = resolve
        })
    )

    const hook = renderHook(() =>
      useVoiceRecorder({
        focusInput: vi.fn(),
        maxRecordingSeconds: 120,
        onTranscribeAudio: vi.fn(async () => ''),
        onTranscript: vi.fn()
      })
    )

    act(() => {
      hook.result.current.dictate()
      hook.result.current.dictate()
    })

    expect(mic.start).toHaveBeenCalledOnce()

    await act(async () => resolveStart?.())
  })

  it('uses a second Dictate toggle to cancel pending microphone acquisition', async () => {
    let resolveStart: (() => void) | undefined
    mic.start.mockImplementation(
      () =>
        new Promise<void>(resolve => {
          resolveStart = resolve
        })
    )

    const hook = renderHook(() =>
      useVoiceRecorder({
        focusInput: vi.fn(),
        maxRecordingSeconds: 120,
        onTranscribeAudio: vi.fn(async () => 'transcript'),
        onTranscript: vi.fn()
      })
    )

    act(() => hook.result.current.dictate())
    act(() => hook.result.current.dictate())

    expect(mic.cancel).toHaveBeenCalledOnce()
    await act(async () => resolveStart?.())
    expect(hook.result.current.voiceStatus).toBe('idle')
  })

  it('cancels and invalidates transcription when unmounted', async () => {
    mic.recording = true
    mic.stop.mockResolvedValue({ audio: new Blob(['audio']), durationMs: 10, heardSpeech: true })
    let resolveTranscription: ((text: string) => void) | undefined

    const onTranscribeAudio = vi.fn(
      () =>
        new Promise<string>(resolve => {
          resolveTranscription = resolve
        })
    )

    const onTranscript = vi.fn()
    const focusInput = vi.fn()

    const hook = renderHook(() =>
      useVoiceRecorder({ focusInput, maxRecordingSeconds: 120, onTranscribeAudio, onTranscript })
    )

    act(() => hook.result.current.dictate())
    await waitFor(() => expect(onTranscribeAudio).toHaveBeenCalledOnce())

    hook.unmount()
    expect(mic.cancel).toHaveBeenCalledOnce()

    await act(async () => resolveTranscription?.('stale transcript'))

    expect(onTranscript).not.toHaveBeenCalled()
    expect(focusInput).not.toHaveBeenCalled()
  })
})
