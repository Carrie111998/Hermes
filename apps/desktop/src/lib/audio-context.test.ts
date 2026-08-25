import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { getRunningAudioContext } from './audio-context'

let context: SuspendedAudioContext | null
let releaseResume: (() => void) | null

class SuspendedAudioContext {
  state = 'suspended'
  private readonly resumePromise: Promise<void>

  constructor() {
    context = this
    this.resumePromise = new Promise<void>(resolve => {
      releaseResume = () => {
        this.state = 'running'
        resolve()
      }
    })
  }

  resume = vi.fn(() => this.resumePromise)
}

function finishResume() {
  const release = releaseResume

  if (!release) {
    throw new Error('Expected a pending AudioContext resume')
  }

  release()
}

describe('getRunningAudioContext', () => {
  beforeEach(() => {
    context = null
    releaseResume = null
    vi.stubGlobal('AudioContext', SuspendedAudioContext)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('does not resolve until a suspended shared context has resumed', async () => {
    const running = getRunningAudioContext()

    await vi.waitFor(() => expect(context).not.toBeNull())
    expect(context?.resume).toHaveBeenCalled()

    let resolved = false
    void running.then(() => {
      resolved = true
    })
    await Promise.resolve()
    expect(resolved).toBe(false)

    finishResume()

    await expect(running).resolves.toBe(context)

    const first = context
    first!.state = 'closed'
    releaseResume = null

    const replacement = getRunningAudioContext()

    await vi.waitFor(() => expect(context).not.toBe(first))
    finishResume()

    await expect(replacement).resolves.toBe(context)
  })
})
