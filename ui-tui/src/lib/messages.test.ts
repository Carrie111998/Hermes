import { describe, expect, it } from 'vitest'

import { appendTranscriptMessage } from './messages.js'

describe('appendTranscriptMessage', () => {
  it('merges adjacent tool-only shelves into one transcript row', () => {
    const out = appendTranscriptMessage([{ kind: 'trail', role: 'system', text: '', tools: ['Terminal("one") ✓'] }], {
      kind: 'trail',
      role: 'system',
      text: '',
      tools: ['Terminal("two") ✓']
    })

    expect(out).toEqual([
      { kind: 'trail', role: 'system', text: '', tools: ['Terminal("one") ✓', 'Terminal("two") ✓'] }
    ])
  })

  it('merges tool shelves into the nearest thinking shelf', () => {
    const out = appendTranscriptMessage(
      [{ kind: 'trail', role: 'system', text: '', thinking: 'plan', tools: ['Terminal("one") ✓'] }],
      { kind: 'trail', role: 'system', text: '', tools: ['Terminal("two") ✓'] }
    )

    expect(out).toEqual([
      { kind: 'trail', role: 'system', text: '', thinking: 'plan', tools: ['Terminal("one") ✓', 'Terminal("two") ✓'] }
    ])
  })

  it('skips an adjacent duplicate user message (#88362)', () => {
    const first = { role: 'user' as const, text: 'hello' }
    const out = appendTranscriptMessage([first], { role: 'user', text: 'hello' })

    expect(out).toHaveLength(1)
    expect(out[0]?.text).toBe('hello')
  })

  it('skips an adjacent duplicate assistant message (#88362)', () => {
    const first = { role: 'assistant' as const, text: 'hi there' }
    const out = appendTranscriptMessage([first], { role: 'assistant', text: 'hi there' })

    expect(out).toHaveLength(1)
  })

  it('does not skip a duplicate that is not adjacent (#88362)', () => {
    const out = appendTranscriptMessage(
      [
        { role: 'user' as const, text: 'hello' },
        { role: 'assistant' as const, text: 'hi' }
      ],
      { role: 'user', text: 'hello' }
    )

    expect(out).toHaveLength(3)
  })

  it('does not skip different user content (#88362)', () => {
    const out = appendTranscriptMessage([{ role: 'user' as const, text: 'first' }], {
      role: 'user',
      text: 'second'
    })

    expect(out).toHaveLength(2)
  })

  it('leaves tool rows to the shelf-merge logic (#88362)', () => {
    const out = appendTranscriptMessage(
      [{ kind: 'trail', role: 'system', text: '', tools: ['Terminal("one") ✓'] }],
      { kind: 'trail', role: 'system', text: '', tools: ['Terminal("one") ✓'] }
    )

    expect(out).toHaveLength(1)
  })
})
