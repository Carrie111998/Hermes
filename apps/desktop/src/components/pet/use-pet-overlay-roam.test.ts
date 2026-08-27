import { describe, expect, it } from 'vitest'

import { clampOverlayRoamBounds, overlayGroundY, overlayRoamLedges } from './use-pet-overlay-roam'

describe('overlay roam geometry', () => {
  it('keeps the visible pet inside a negative-coordinate display', () => {
    const area = { height: 1040, width: 1920, x: -1920, y: 0 }

    expect(clampOverlayRoamBounds(area, { height: 300, width: 240, x: -2300, y: 900 }, 64, 69)).toEqual({
      height: 300,
      width: 240,
      x: -2008,
      y: 768
    })
    expect(clampOverlayRoamBounds(area, { height: 300, width: 240, x: 200, y: -500 }, 64, 69)).toEqual({
      height: 300,
      width: 240,
      x: -152,
      y: -207
    })
  })

  it('places the overlay so its visible feet meet the usable display floor', () => {
    expect(overlayGroundY({ height: 1040, width: 1920, x: 0, y: 24 }, 300)).toBe(792)
  })

  it('turns visible app-window top edges into ledges and removes covered spans', () => {
    const ledges = overlayRoamLedges(
      {
        windows: [
          { height: 300, width: 300, x: 100, y: 200 },
          { height: 300, width: 500, x: 50, y: 350 }
        ],
        workArea: { height: 800, width: 1200, x: 0, y: 0 }
      },
      240,
      64,
      70
    )

    expect(ledges).toEqual([
      { left: -88, right: 1048, y: 800 },
      { left: 12, right: 248, y: 200 },
      { left: 312, right: 398, y: 350 }
    ])
  })

  it('falls back to the display floor when a maximized front window covers everything', () => {
    const ledges = overlayRoamLedges(
      {
        windows: [
          { height: 800, width: 1200, x: 0, y: 0 },
          { height: 300, width: 500, x: 100, y: 300 }
        ],
        workArea: { height: 800, width: 1200, x: 0, y: 0 }
      },
      240,
      64,
      70
    )

    expect(ledges).toEqual([{ left: -88, right: 1048, y: 800 }])
  })
})
