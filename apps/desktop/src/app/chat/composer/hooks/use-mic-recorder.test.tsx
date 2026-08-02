import { act, cleanup, renderHook } from '@testing-library/react'
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

function deferred<T>() {
  let resolve!: (value: T) => void

  const promise = new Promise<T>(next => {
    resolve = next
  })

  return { promise, resolve }
}

function fakeStream() {
  const stop = vi.fn()

  const stream = {
    getTracks: () => [{ stop }]
  } as unknown as MediaStream

  return { stop, stream }
}

class FakeMediaRecorder {
  static instances: FakeMediaRecorder[] = []
  static isTypeSupported = vi.fn(() => true)

  mimeType = 'audio/webm'
  ondataavailable: ((event: BlobEvent) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  onstop: (() => void) | null = null
  start = vi.fn(() => {
    this.state = 'recording'
  })
  state: RecordingState = 'inactive'
  stop = vi.fn(() => {
    this.state = 'inactive'
    this.onstop?.()
  })

  constructor(readonly stream: MediaStream) {
    FakeMediaRecorder.instances.push(this)
  }
}

describe('useMicRecorder async acquisition ownership', () => {
  beforeEach(() => {
    FakeMediaRecorder.instances = []
    vi.stubGlobal('MediaRecorder', FakeMediaRecorder)
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia: vi.fn() }
    })
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { requestMicrophoneAccess: vi.fn(async () => true) }
    })
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it.each(['stale-first', 'current-first'] as const)(
    'keeps only the current start active when streams resolve %s',
    async resolveOrder => {
      const acquisitionA = deferred<MediaStream>()
      const acquisitionB = deferred<MediaStream>()
      const streamA = fakeStream()
      const streamB = fakeStream()
      vi.mocked(navigator.mediaDevices.getUserMedia)
        .mockReturnValueOnce(acquisitionA.promise)
        .mockReturnValueOnce(acquisitionB.promise)

      const hook = renderHook(() => useMicRecorder(copy))
      let startA!: Promise<void>
      let startB!: Promise<void>

      await act(async () => {
        startA = hook.result.current.handle.start()
        await Promise.resolve()
        hook.result.current.handle.cancel()
        startB = hook.result.current.handle.start()
        await Promise.resolve()
      })

      await act(async () => {
        if (resolveOrder === 'stale-first') {
          acquisitionA.resolve(streamA.stream)
          await startA
          acquisitionB.resolve(streamB.stream)
          await startB
        } else {
          acquisitionB.resolve(streamB.stream)
          await startB
          acquisitionA.resolve(streamA.stream)
          await startA
        }
      })

      const recorderA = FakeMediaRecorder.instances.find(recorder => recorder.stream === streamA.stream)
      const recorderB = FakeMediaRecorder.instances.find(recorder => recorder.stream === streamB.stream)

      expect(streamA.stop).toHaveBeenCalledTimes(1)
      expect(recorderA).toBeUndefined()
      expect(streamB.stop).not.toHaveBeenCalled()
      expect(recorderB?.state).toBe('recording')
      expect(hook.result.current.recording).toBe(true)
    }
  )

  it('stops a stream acquired after the hook unmounts', async () => {
    const acquisition = deferred<MediaStream>()
    const staleStream = fakeStream()
    vi.mocked(navigator.mediaDevices.getUserMedia).mockReturnValueOnce(acquisition.promise)

    const hook = renderHook(() => useMicRecorder(copy))
    let start!: Promise<void>

    await act(async () => {
      start = hook.result.current.handle.start()
      await Promise.resolve()
    })
    hook.unmount()

    await act(async () => {
      acquisition.resolve(staleStream.stream)
      await start
    })

    expect(staleStream.stop).toHaveBeenCalledTimes(1)
    expect(FakeMediaRecorder.instances).toHaveLength(0)
  })
})
