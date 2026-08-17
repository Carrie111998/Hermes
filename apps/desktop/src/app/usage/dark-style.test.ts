import { describe, expect, it } from 'vitest'

import { usageDarkStyle } from './dark-style'

describe('Usage dashboard conventions', () => {
  it('keeps the Usage surface midnight-black while retaining skin-derived semantic colors', () => {
    const style = usageDarkStyle('dark')

    expect(style.background).toBe('#05070a')
    expect(style['--dt-background']).toBe('#05070a')
    expect(style['--ui-bg-card']).toBe('#080c11')
    expect(style['--ui-bg-chrome']).toBe('#05070a')
    expect(style['--ui-bg-elevated']).toBe('#0b1118')
    expect(style['--ui-accent-secondary']).toBeTruthy()
    expect(style['--ui-text-primary']).toBeTruthy()
  })
})
