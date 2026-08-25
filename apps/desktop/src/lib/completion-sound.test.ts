import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $hapticsMuted } from '@/store/haptics'

import { playCompletionSound, previewCompletionSound } from './completion-sound'

const { getRunningAudioContext, ownsAmbientCue } = vi.hoisted(() => ({
  getRunningAudioContext: vi.fn(),
  ownsAmbientCue: vi.fn()
}))

vi.mock('@/lib/audio-context', () => ({ getRunningAudioContext }))
vi.mock('@/store/ambient', () => ({ ownsAmbientCue }))

class FakeParam {
  setValueAtTime = vi.fn()
  exponentialRampToValueAtTime = vi.fn()
}

class FakeGain {
  gain = new FakeParam()
  connect = vi.fn()
}

class FakeFilter {
  type = 'lowpass'
  frequency = new FakeParam()
  Q = new FakeParam()
  connect = vi.fn()
}

class FakeConvolver {
  buffer: AudioBuffer | null = null
  connect = vi.fn()
}

class FakeOscillator {
  type = 'sine'
  frequency = new FakeParam()
  connect = vi.fn()
  start = vi.fn()
  stop = vi.fn()
}

let gains: FakeGain[]
let oscillators: FakeOscillator[]
let releaseContext: (() => void) | null
let releaseOwnership: ((owned: boolean) => void) | null

const context = {
  currentTime: 0,
  destination: {},
  sampleRate: 100,
  createGain: () => {
    const gain = new FakeGain()
    gains.push(gain)

    return gain
  },
  createBiquadFilter: () => new FakeFilter(),
  createConvolver: () => new FakeConvolver(),
  createBuffer: (channels: number, length: number) => {
    const data = Array.from({ length: channels }, () => new Float32Array(length))

    return { getChannelData: (channel: number) => data[channel] }
  },
  createOscillator: () => {
    const oscillator = new FakeOscillator()
    oscillators.push(oscillator)

    return oscillator
  }
} as unknown as AudioContext

describe('previewCompletionSound', () => {
  beforeEach(() => {
    gains = []
    oscillators = []
    releaseContext = null
    releaseOwnership = null
    getRunningAudioContext.mockReset()
    getRunningAudioContext.mockResolvedValue(context)
    ownsAmbientCue.mockReset()
    ownsAmbientCue.mockResolvedValue(true)
    $hapticsMuted.set(false)
  })

  afterEach(() => {
    $hapticsMuted.set(false)
  })

  it('waits for a runnable audio context before scheduling an audible preview', async () => {
    getRunningAudioContext.mockReturnValue(
      new Promise<AudioContext | null>(resolve => {
        releaseContext = () => resolve(context)
      })
    )

    const playing = previewCompletionSound(8)

    expect(playing).toBeInstanceOf(Promise)
    expect(oscillators).toHaveLength(0)

    releaseContext?.()
    await playing

    expect(oscillators).toHaveLength(2)
    expect(gains[0].gain.setValueAtTime).toHaveBeenCalledWith(1.15, expect.any(Number))
  })

  it('stays silent when muted while the audio context is resuming', async () => {
    getRunningAudioContext.mockReturnValue(
      new Promise<AudioContext | null>(resolve => {
        releaseContext = () => resolve(context)
      })
    )

    const playing = playCompletionSound('session-1')
    $hapticsMuted.set(true)
    releaseContext?.()
    await playing

    expect(ownsAmbientCue).not.toHaveBeenCalled()
    expect(oscillators).toHaveLength(0)
  })

  it('stays silent when muted while cross-window ownership is pending', async () => {
    ownsAmbientCue.mockReturnValue(
      new Promise<boolean>(resolve => {
        releaseOwnership = resolve
      })
    )

    const playing = playCompletionSound('session-1')
    await vi.waitFor(() => expect(ownsAmbientCue).toHaveBeenCalledOnce())
    $hapticsMuted.set(true)
    releaseOwnership?.(true)
    await playing

    expect(oscillators).toHaveLength(0)
  })
})
