import { describe, expect, it } from 'vitest'

import { parseDelegateWaveWake, parseDelegateWaveWakeEvent } from './delegate-wave-wake'

const marker = '[delegate-wave-wake:wake_123]'

describe('parseDelegateWaveWake', () => {
  it.each([
    [
      `The delegate-wave session working on "ship it" finished and its result is on the branch.\n\n${marker}`,
      'completed',
      'ship it',
      undefined
    ],
    [
      `The delegate-wave session working on "ship it" has a finished, validated candidate waiting for a person.\n\n${marker}`,
      'ready',
      'ship it',
      undefined
    ],
    [
      `The delegate-wave session working on "ship it" failed.\n\nTests still fail.\n\nUse session_poll now.\n\n${marker}`,
      'failed',
      'ship it',
      'Tests still fail.'
    ],
    [
      `The delegate-wave session working on "ship it" needs an answer before it can continue.\n\nWhich API?\n\nWhy it matters: It changes the design.\n\n${marker}`,
      'question',
      'ship it',
      'Which API?'
    ]
  ])('parses a %s wake', (text, kind, task, detail) => {
    expect(parseDelegateWaveWake(text)).toEqual({ kind, task, ...(detail ? { detail } : {}) })
  })

  it('does not style ordinary messages', () => {
    expect(parseDelegateWaveWake('ship it')).toBeNull()
  })

  it('requires the durable event type even for identical wake-shaped text', () => {
    const wake = `The delegate-wave session working on "ship it" finished and its result is on the branch.\n\n${marker}`

    expect(parseDelegateWaveWakeEvent(wake, 'delegate_wave_wake')).toEqual({ kind: 'completed', task: 'ship it' })
    expect(parseDelegateWaveWakeEvent(wake, undefined)).toBeNull()
  })
})
