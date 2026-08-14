import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useVoiceRecorder } from './use-voice-recorder'

const mocks = vi.hoisted(() => {
  let recording = true
  let resolveStop: ((value: { audio: Blob; durationMs: number; heardSpeech: boolean }) => void) | null = null

  return {
    handle: {
      cancel: vi.fn(),
      start: vi.fn(async () => undefined),
      stop: vi.fn(
        () =>
          new Promise<{ audio: Blob; durationMs: number; heardSpeech: boolean }>(resolve => {
            resolveStop = resolve
          })
      )
    },
    isRecording: () => recording,
    reset() {
      recording = true
      resolveStop = null
    },
    finishRecording() {
      recording = false
      resolveStop?.({ audio: new Blob(['voice']), durationMs: 1000, heardSpeech: true })
    }
  }
})

vi.mock('./use-mic-recorder', () => ({
  useMicRecorder: () => ({ handle: mocks.handle, level: 0, recording: mocks.isRecording() })
}))

const notifyError = vi.fn()

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: (...args: unknown[]) => notifyError(...args)
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

function deferredTranscript() {
  let resolve!: (text: string) => void
  let reject!: (error: Error) => void
  const promise = new Promise<string>((res, rej) => {
    resolve = res
    reject = rej
  })

  return { promise, reject, resolve }
}

describe('useVoiceRecorder transcription cancellation', () => {
  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
    mocks.reset()
  })

  it('aborts transcription and discards a late result', async () => {
    const pending = deferredTranscript()
    const onTranscript = vi.fn()
    const onTranscribeAudio = vi.fn((_audio: Blob, signal?: AbortSignal) => {
      expect(signal?.aborted).toBe(false)

      return pending.promise
    })
    const hook = renderHook(() =>
      useVoiceRecorder({ focusInput: vi.fn(), maxRecordingSeconds: 120, onTranscript, onTranscribeAudio })
    )

    act(() => hook.result.current.dictate())
    act(() => mocks.finishRecording())
    hook.rerender()
    await waitFor(() => expect(hook.result.current.voiceStatus).toBe('transcribing'))

    const signal = onTranscribeAudio.mock.calls[0]?.[1]
    act(() => hook.result.current.dictate())

    expect(signal?.aborted).toBe(true)
    expect(hook.result.current.voiceStatus).toBe('idle')

    await act(async () => pending.resolve('stale transcript'))

    expect(onTranscript).not.toHaveBeenCalled()
    expect(notifyError).not.toHaveBeenCalled()
  })

  it('suppresses an abort rejection after cancellation', async () => {
    const pending = deferredTranscript()
    const hook = renderHook(() =>
      useVoiceRecorder({
        focusInput: vi.fn(),
        maxRecordingSeconds: 120,
        onTranscript: vi.fn(),
        onTranscribeAudio: () => pending.promise
      })
    )

    act(() => hook.result.current.dictate())
    act(() => mocks.finishRecording())
    hook.rerender()
    await waitFor(() => expect(hook.result.current.voiceStatus).toBe('transcribing'))
    act(() => hook.result.current.dictate())
    await act(async () => pending.reject(new DOMException('canceled', 'AbortError')))

    expect(notifyError).not.toHaveBeenCalled()
  })
})
