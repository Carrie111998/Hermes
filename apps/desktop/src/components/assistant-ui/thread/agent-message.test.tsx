import { describe, expect, it } from 'vitest'

import { AGENT_MESSAGE_RE } from './user-message'

// Agent-to-agent deliveries render as an attributed inter-agent card, not a
// user bubble. This pins the detection contract: both the Bot Mode prefix
// ("Message from 🤖 <sender>: …") and the legacy bracket form match, and
// human prose that merely mentions the phrase does not.
describe('agent message detection', () => {
  it('matches the Bot Mode delivery prefix with sender and body', () => {
    const m = AGENT_MESSAGE_RE.exec('Message from 🤖 Hermes: hello there')

    expect(m?.[1]?.trim()).toBe('Hermes')
    expect(m?.[3]).toBe('hello there')
  })

  it('matches without the robot emoji', () => {
    const m = AGENT_MESSAGE_RE.exec('Message from Turquoise: ready to work')

    expect(m?.[1]?.trim()).toBe('Turquoise')
    expect(m?.[3]).toBe('ready to work')
  })

  it('matches the legacy bracket form', () => {
    const m = AGENT_MESSAGE_RE.exec("[Message from agent 'turqoise'] ping")

    expect(m?.[2]).toBe('turqoise')
    expect(m?.[3]).toBe('ping')
  })

  it('spans multi-line bodies', () => {
    const m = AGENT_MESSAGE_RE.exec('Message from 🤖 Dev: line one\nline two')

    expect(m?.[3]).toBe('line one\nline two')
  })

  it('does not match prose that merely contains the phrase', () => {
    expect(AGENT_MESSAGE_RE.test('I got a Message from 🤖 Hermes: earlier')).toBe(false)
    expect(AGENT_MESSAGE_RE.test('can you explain what Message from means?')).toBe(false)
  })
})
