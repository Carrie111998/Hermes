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

  it('does not leak across sessions when the same text is spoken in one but not the other', () => {
    markTextSpoken('Done.', 'session-a')

    expect(wasTextAlreadySpoken('Done.', 'session-a')).toBe(true)
    expect(wasTextAlreadySpoken('Done.', 'session-b')).toBe(false)
  })

  it('dedupes independently per session', () => {
    markTextSpoken('Fixed.', 'session-a')
    markTextSpoken('Yes.', 'session-b')

    expect(wasTextAlreadySpoken('Fixed.', 'session-a')).toBe(true)
    expect(wasTextAlreadySpoken('Fixed.', 'session-b')).toBe(false)
    expect(wasTextAlreadySpoken('Yes.', 'session-b')).toBe(true)
  })
})
