import { describe, expect, it } from 'vitest'

import { initialOverlayBounds, overlayWindowSize } from './pet-overlay'

describe('pet overlay geometry', () => {
  it('keeps the existing padded minimum around a normally sized pet', () => {
    expect(overlayWindowSize(192, 208, 0.5)).toEqual({ height: 304, width: 240 })
  })

  it('opens the settings-driven overlay near the lower-left without leaving a short viewport', () => {
    expect(initialOverlayBounds(192, 208, 0.5, 700)).toEqual({
      height: 304,
      width: 240,
      x: 24,
      y: 372
    })

    expect(initialOverlayBounds(192, 208, 2, 600).y).toBe(24)
  })
})
