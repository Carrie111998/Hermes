import { describe, expect, it, vi } from 'vitest'

import { isolateTileWheel } from './tile-scroll'

describe('isolateTileWheel', () => {
  it('stops the wheel from reaching sibling tile scrollers', () => {
    const event = { stopPropagation: vi.fn() }

    isolateTileWheel(event)

    expect(event.stopPropagation).toHaveBeenCalledOnce()
  })
})
