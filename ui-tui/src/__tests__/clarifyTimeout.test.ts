import { describe, expect, it } from 'vitest'

import { clarifyDeadlineExpired, clarifyTimeoutNotice, mergePreservedClarifyDraft } from '../lib/clarifyTimeout.js'

describe('clarifyTimeoutNotice', () => {
  it('makes the finite response window visible from the start', () => {
    expect(clarifyTimeoutNotice(3600)).toEqual({
      text: 'response times out in 1:00:00',
      urgent: false
    })
  })

  it('warns prominently during the final minute', () => {
    expect(clarifyTimeoutNotice(59)).toEqual({
      text: '⚠ response times out in 0:59 — submit now; your draft will be preserved',
      urgent: true
    })
  })

  it('does not show a deadline for unlimited prompts', () => {
    expect(clarifyTimeoutNotice(undefined)).toBeNull()
  })
})

describe('clarifyDeadlineExpired', () => {
  it('rejects submission at the deadline before the countdown interval ticks', () => {
    expect(clarifyDeadlineExpired(10_000, 10_000)).toBe(true)
  })

  it('allows submission immediately before the deadline', () => {
    expect(clarifyDeadlineExpired(10_000, 9_999)).toBe(false)
  })

  it('allows submission for unlimited prompts', () => {
    expect(clarifyDeadlineExpired(undefined, 20_000)).toBe(false)
  })
})

describe('mergePreservedClarifyDraft', () => {
  it('restores the clarify draft without overwriting an existing composer draft', () => {
    expect(mergePreservedClarifyDraft('existing note', 'long-form answer')).toBe(
      'existing note\n\nlong-form answer'
    )
  })

  it('restores the clarify draft directly when the composer is empty', () => {
    expect(mergePreservedClarifyDraft('', 'long-form answer')).toBe('long-form answer')
  })
})
