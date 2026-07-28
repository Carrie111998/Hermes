import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { GlyphSpinner } from './glyph-spinner'

describe('GlyphSpinner', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('renders a stable status glyph without scheduling animation work', () => {
    render(<GlyphSpinner ariaLabel="Working" />)

    const glyph = screen.getByRole('status', { name: 'Working' })

    expect(glyph.textContent).toHaveLength(1)
    expect(vi.getTimerCount()).toBe(0)
  })
})
