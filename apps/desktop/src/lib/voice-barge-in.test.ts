import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { monitorSpeechDuringPlayback } from './voice-barge-in'

class FakeMediaRecorder {
  static instances: FakeMediaRecorder[] = []
  static isTypeSupported = () => false

  mimeType = 'audio/webm'
  ondataavailable: ((event: { data: Blob }) => void) | null = null
  onstop: (() => void) | null = null
  state: RecordingState = 'inactive'

  constructor(_stream: MediaStream) {
    FakeMediaRecorder.instances.push(this)
  }

  start() {
    if (FakeMediaRecorder.instances.length === 2) {
      throw new DOMException('rotation failed', 'InvalidStateError')
    }

    this.state = 'recording'
  }

  stop() {
    this.state = 'inactive'
  }
}

class FakeAudioContext {
  createAnalyser() {
    return {
      fftSize: 0,
      getByteTimeDomainData: (data: Uint8Array) => data.fill(128)
    }
  }

  createMediaStreamSource() {
    return { connect: vi.fn() }
  }

  close = vi.fn(async () => undefined)
}

describe('monitorSpeechDuringPlayback recorder rotation', () => {
  const originalMediaDevices = navigator.mediaDevices
  let now = 0
  let frames: FrameRequestCallback[]

  beforeEach(() => {
    FakeMediaRecorder.instances = []
    frames = []
    now = 0
    vi.stubGlobal('MediaRecorder', FakeMediaRecorder)
    vi.stubGlobal('AudioContext', FakeAudioContext)
    vi.spyOn(Date, 'now').mockImplementation(() => now)
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation(callback => {
      frames.push(callback)

      return frames.length
    })
    vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => undefined)
  })

  afterEach(() => {
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: originalMediaDevices })
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('keeps the monitor disposable when a pre-roll recorder restart throws', async () => {
    const stopTrack = vi.fn()
    const media = { getTracks: () => [{ stop: stopTrack }] } as unknown as MediaStream

    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia: vi.fn(async () => media) }
    })

    const dispose = monitorSpeechDuringPlayback({ isPlaying: () => false, onSpeech: vi.fn() })

    await vi.waitFor(() => expect(FakeMediaRecorder.instances).toHaveLength(1))
    expect(frames).toHaveLength(1)

    now = 6_000
    expect(() => frames.shift()?.(now)).not.toThrow()

    dispose()
    expect(stopTrack).toHaveBeenCalledOnce()
  })
})
