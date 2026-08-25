import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $hapticsMuted } from '@/store/haptics'

import { playApprovalSound } from './approval-sound'

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

class FakeOscillator {
  type = 'sine'
  frequency = new FakeParam()
  connect = vi.fn()
  start = vi.fn()
  stop = vi.fn()
}

class FakeGain {
  gain = new FakeParam()
  connect = vi.fn()
}

let gains: FakeGain[]
let oscillators: FakeOscillator[]
let releaseContext: ((context: AudioContext | null) => void) | null
let releaseOwnership: ((owned: boolean) => void) | null

const context = {
  currentTime: 0,
  destination: {},
  createGain: () => {
    const gain = new FakeGain()
    gains.push(gain)

    return gain
  },
  createOscillator: () => {
    const oscillator = new FakeOscillator()
    oscillators.push(oscillator)

    return oscillator
  }
} as unknown as AudioContext

describe('playApprovalSound', () => {
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

  it('plays a repeated two-tone approval alarm in one window', async () => {
    await playApprovalSound('session-1:req-1')

    expect(ownsAmbientCue).toHaveBeenCalledWith('approval-sound:session-1:req-1')
    expect(oscillators).toHaveLength(4)
    expect(oscillators.map(oscillator => oscillator.frequency.setValueAtTime.mock.calls[0][0])).toEqual([
      659.25,
      987.77,
      659.25,
      987.77
    ])
    expect(gains[0].gain.setValueAtTime).toHaveBeenCalledWith(0.9, expect.any(Number))
  })

  it('stays silent when another window owns the cue', async () => {
    ownsAmbientCue.mockResolvedValue(false)

    await playApprovalSound('session-1:req-1')

    expect(getRunningAudioContext).toHaveBeenCalledOnce()
    expect(oscillators).toHaveLength(0)
  })

  it('stays silent when shared sounds are muted', async () => {
    $hapticsMuted.set(true)

    await playApprovalSound('session-1:req-1')

    expect(ownsAmbientCue).not.toHaveBeenCalled()
    expect(getRunningAudioContext).not.toHaveBeenCalled()
  })

  it('stays silent when muted while the audio context is resuming', async () => {
    getRunningAudioContext.mockReturnValue(
      new Promise<AudioContext | null>(resolve => {
        releaseContext = resolve
      })
    )

    const playing = playApprovalSound('session-1:req-resume')
    $hapticsMuted.set(true)
    releaseContext?.(context)
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

    const playing = playApprovalSound('session-1:req-owner')
    await vi.waitFor(() => expect(ownsAmbientCue).toHaveBeenCalledOnce())
    $hapticsMuted.set(true)
    releaseOwnership?.(true)
    await playing

    expect(oscillators).toHaveLength(0)
  })

  it('does not throw when no running audio context is available', async () => {
    getRunningAudioContext.mockResolvedValue(null)

    await expect(playApprovalSound()).resolves.toBeUndefined()
    expect(ownsAmbientCue).not.toHaveBeenCalled()
    expect(oscillators).toHaveLength(0)
  })
})
