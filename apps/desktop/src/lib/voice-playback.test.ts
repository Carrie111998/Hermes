import { describe, expect, it } from 'vitest'

import { markTextSpoken, wasTextAlreadySpoken } from './voice-playback'

describe('wasTextAlreadySpoken', () => {
  it('is false before anything has been marked spoken', () => {
    expect(wasTextAlreadySpoken('a reply nobody has spoken yet')).toBe(false)
  })

  it('is true for the exact text just marked spoken, ignoring surrounding whitespace', () => {
    markTextSpoken('  the reply  ')

    expect(wasTextAlreadySpoken('the reply')).toBe(true)
    expect(wasTextAlreadySpoken(' the reply ')).toBe(true)
  })

  it('is false for a different reply', () => {
    markTextSpoken('first reply')

    expect(wasTextAlreadySpoken('second reply')).toBe(false)
  })
})
