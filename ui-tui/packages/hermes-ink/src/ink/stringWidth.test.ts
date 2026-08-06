import { describe, expect, it } from 'vitest'

import { stringWidth } from './stringWidth.js'

describe('stringWidth emoji presentation', () => {
  it('keeps text-default symbols narrow', () => {
    expect(stringWidth('⚔')).toBe(1)
    expect(stringWidth('⚔︎')).toBe(1)
    expect(stringWidth('⚕')).toBe(1)
    expect(stringWidth('⚕︎')).toBe(1)
    expect(stringWidth('⚠')).toBe(1)
  })

  it('keeps explicit and default emoji wide', () => {
    expect(stringWidth('⚔️')).toBe(2)
    expect(stringWidth('⚠️')).toBe(2)
    expect(stringWidth('⌚')).toBe(2)
  })
})
