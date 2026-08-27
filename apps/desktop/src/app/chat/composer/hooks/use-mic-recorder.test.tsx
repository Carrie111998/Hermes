import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { type MicRecorderErrorCopy, useMicRecorder } from './use-mic-recorder'

const copy: MicRecorderErrorCopy = {
  microphoneAccessDenied: 'denied',
  microphoneConstraintsUnsupported: 'constraints',
  microphoneInUse: 'in use',
  microphonePermissionDenied: 'permission',
  microphoneStartFailed: 'failed',
  microphoneUnsupported: 'unsupported',
  noMicrophone: 'missing'
}

class FakeMediaRecorder {
  static instances: FakeMediaRecorder[] = []
  static isTypeSupported = () => false

  mimeType = 'audio/webm'
  ondataavailable: ((event: { data: Blob }) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  onstop: (() => void) | null = null
  state: RecordingState = 'inactive'

  constructor(_stream: MediaStream) {
    FakeMediaRecorder.instances.push(this)
  }

  start() {
    this.state = 'recording'
  }

  stop() {
    this.state = 'inactive'
  }
}

const stream = () => {
  const stop = vi.fn()

  return { media: { getTracks: () => [{ stop }] } as unknown as MediaStream, stop }
}

describe('useMicRecorder generations', () => {
  const originalMediaRecorder = globalThis.MediaRecorder
  const originalMediaDevices = navigator.mediaDevices

  beforeEach(() => {
    FakeMediaRecorder.instances = []
    Object.defineProperty(globalThis, 'MediaRecorder', { configurable: true, value: FakeMediaRecorder })
  })

  afterEach(() => {
    Object.defineProperty(globalThis, 'MediaRecorder', { configurable: true, value: originalMediaRecorder })
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: originalMediaDevices })
  })

  it('stop cancels a pending microphone acquisition', async () => {
    let resolveStream: ((value: MediaStream) => void) | undefined

    const pendingStream = new Promise<MediaStream>(resolve => {
      resolveStream = resolve
    })

    const acquired = stream()

    const getUserMedia = vi.fn(() => pendingStream)

    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia }
    })

    const hook = renderHook(() => useMicRecorder(copy))
    let starting: Promise<void> | undefined

    act(() => {
      starting = hook.result.current.handle.start()
    })
    await waitFor(() => expect(getUserMedia).toHaveBeenCalledOnce())
    await expect(hook.result.current.handle.stop()).resolves.toBeNull()
    resolveStream?.(acquired.media)
    await act(async () => starting)

    expect(acquired.stop).toHaveBeenCalledOnce()
    expect(hook.result.current.recording).toBe(false)
  })

  it('ignores a stale onstop after a replacement recorder starts', async () => {
    const first = stream()
    const second = stream()
    const getUserMedia = vi.fn().mockResolvedValueOnce(first.media).mockResolvedValueOnce(second.media)

    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia } })

    const hook = renderHook(() => useMicRecorder(copy))

    await act(async () => hook.result.current.handle.start())
    const firstRecorder = FakeMediaRecorder.instances[0]!
    const staleOnStop = firstRecorder.onstop
    const firstStop = hook.result.current.handle.stop()

    act(() => hook.result.current.handle.cancel())
    await expect(firstStop).resolves.toBeNull()
    expect(firstRecorder.onstop).toBeNull()

    await act(async () => hook.result.current.handle.start())
    expect(hook.result.current.recording).toBe(true)

    act(() => staleOnStop?.())

    expect(second.stop).not.toHaveBeenCalled()
    expect(hook.result.current.recording).toBe(true)
  })
})
