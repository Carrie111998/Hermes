import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { type MicRecorderErrorCopy, useMicRecorder } from './use-mic-recorder'

const copy: MicRecorderErrorCopy = {
  microphoneAccessDenied: 'access denied',
  microphoneConstraintsUnsupported: 'constraints unsupported',
  microphoneInUse: 'in use',
  microphonePermissionDenied: 'permission denied',
  microphoneStartFailed: 'start failed',
  microphoneUnsupported: 'unsupported',
  noMicrophone: 'no microphone'
}

class FakeMediaRecorder {
  static instances: FakeMediaRecorder[] = []
  static isTypeSupported = () => true

  mimeType: string
  ondataavailable: ((event: { data: Blob }) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  onstop: (() => void) | null = null
  startTimeslice: number | undefined
  state: RecordingState = 'inactive'

  constructor(_stream: MediaStream, options?: MediaRecorderOptions) {
    this.mimeType = options?.mimeType ?? 'audio/webm'
    FakeMediaRecorder.instances.push(this)
  }

  emit(data: Blob) {
    this.ondataavailable?.({ data })
  }

  start(timeslice?: number) {
    this.startTimeslice = timeslice
    this.state = 'recording'
  }

  stop() {
    this.state = 'inactive'
    this.onstop?.()
  }
}

describe('useMicRecorder partial audio', () => {
  beforeEach(() => {
    FakeMediaRecorder.instances = []
    vi.stubGlobal('MediaRecorder', FakeMediaRecorder)
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: {
        getUserMedia: vi.fn(async () => ({ getTracks: () => [{ stop: vi.fn() }] }))
      }
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('emits the accumulated recording at the requested caption interval', async () => {
    const onPartialAudio = vi.fn()
    const { result, unmount } = renderHook(() => useMicRecorder(copy))

    await act(async () => {
      await result.current.handle.start({ onPartialAudio, partialAudioMs: 2_500 })
    })

    const recorder = FakeMediaRecorder.instances[0]!
    expect(recorder.startTimeslice).toBe(2_500)

    const firstChunk = new Blob(['first'], { type: 'audio/webm' })
    const secondChunk = new Blob(['second'], { type: 'audio/webm' })

    act(() => recorder.emit(firstChunk))
    act(() => recorder.emit(secondChunk))

    expect(onPartialAudio).toHaveBeenCalledTimes(2)
    expect(onPartialAudio.mock.calls[1]?.[0]).toBeInstanceOf(Blob)
    expect(onPartialAudio.mock.calls[1]?.[0].size).toBe(firstChunk.size + secondChunk.size)

    unmount()
  })

  it('stops emitting accumulated partial audio after the configured preview limit', async () => {
    const now = vi.spyOn(Date, 'now').mockReturnValue(0)
    const onPartialAudio = vi.fn()
    const { result, unmount } = renderHook(() => useMicRecorder(copy))

    await act(async () => {
      await result.current.handle.start({
        onPartialAudio,
        partialAudioMaxMs: 10_000,
        partialAudioMs: 2_500
      })
    })

    const recorder = FakeMediaRecorder.instances[0]!

    act(() => recorder.emit(new Blob(['first'], { type: 'audio/webm' })))
    now.mockReturnValue(10_001)
    act(() => recorder.emit(new Blob(['second'], { type: 'audio/webm' })))

    expect(onPartialAudio).toHaveBeenCalledTimes(1)

    unmount()
    now.mockRestore()
  })

  it.each(['cancel', 'unmount'] as const)(
    'stops a stream that arrives after %s while microphone startup is pending',
    async action => {
      let resolveStream!: (stream: MediaStream) => void

      const stopTrack = vi.fn()
      const stream = { getTracks: () => [{ stop: stopTrack }] } as unknown as MediaStream

      const getUserMedia = vi.fn(
        () =>
          new Promise<MediaStream>(resolve => {
            resolveStream = resolve
          })
      )

      Object.defineProperty(navigator, 'mediaDevices', {
        configurable: true,
        value: { getUserMedia }
      })

      const { result, unmount } = renderHook(() => useMicRecorder(copy))
      let startRequest!: Promise<void>

      act(() => {
        startRequest = result.current.handle.start()
      })

      await vi.waitFor(() => expect(getUserMedia).toHaveBeenCalledTimes(1))

      if (action === 'cancel') {
        act(() => result.current.handle.cancel())
      } else {
        unmount()
      }

      resolveStream(stream)
      await startRequest

      expect(stopTrack).toHaveBeenCalledTimes(1)
      expect(FakeMediaRecorder.instances).toHaveLength(0)

      if (action === 'cancel') {
        unmount()
      }
    }
  )

  it('detaches an active recorder before unmount so late data cannot upload', async () => {
    const stopTrack = vi.fn()
    const onPartialAudio = vi.fn()

    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia: vi.fn(async () => ({ getTracks: () => [{ stop: stopTrack }] })) }
    })

    const { result, unmount } = renderHook(() => useMicRecorder(copy))

    await act(async () => {
      await result.current.handle.start({ onPartialAudio, partialAudioMs: 2_500 })
    })

    const recorder = FakeMediaRecorder.instances[0]!
    const lateDataHandler = recorder.ondataavailable

    unmount()

    expect(recorder.state).toBe('inactive')
    expect(recorder.ondataavailable).toBeNull()
    expect(stopTrack).toHaveBeenCalledTimes(1)

    lateDataHandler?.({ data: new Blob(['late'], { type: 'audio/webm' }) })
    expect(onPartialAudio).not.toHaveBeenCalled()
  })
})
