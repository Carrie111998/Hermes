import { renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  clampOverlayRoamBounds,
  overlayGroundY,
  overlayHopArcHeight,
  overlayHopMaxTravel,
  overlayHopRange,
  overlayRoamLedges,
  pickOverlayHopTarget,
  usePetOverlayRoam
} from './use-pet-overlay-roam'

const originalDesktop = window.hermesDesktop

afterEach(() => {
  vi.restoreAllMocks()
  Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: originalDesktop })
})

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

  it('limits sideways hops and uses a taller arc', () => {
    const ledge = { left: 0, right: 1200, y: 700 }
    const range = overlayHopRange(ledge, ledge, 500, 64)

    expect(overlayHopMaxTravel(64)).toBe(96)
    expect(range).toEqual({ left: 404, right: 596, y: 700 })
    expect(pickOverlayHopTarget(range!, 500, () => 1)).toBe(596)
    expect(overlayHopArcHeight(70, 0)).toBe(105)
    expect(overlayHopArcHeight(70, 100)).toBe(160)
  })

  it('rejects a destination ledge that is too far away for one hop', () => {
    expect(
      overlayHopRange({ left: 0, right: 800, y: 700 }, { left: 300, right: 700, y: 500 }, 100, 64)
    ).toBeNull()
  })

  it('replans immediately when the drag completion key changes', async () => {
    const roamEnvironment = vi.fn(async () => ({
      windows: [],
      workArea: { height: 800, width: 1200, x: 0, y: 0 }
    }))

    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        petOverlay: {
          control: vi.fn(),
          roamEnvironment,
          setBounds: vi.fn()
        }
      } as unknown as Window['hermesDesktop']
    })
    vi.spyOn(window, 'requestAnimationFrame').mockReturnValue(1)
    vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => {})

    const isInteracting = () => false

    const { rerender, unmount } = renderHook(
      ({ replanKey }) =>
        usePetOverlayRoam({ enabled: true, isInteracting, loopMs: 1100, petH: 70, petW: 64, replanKey }),
      { initialProps: { replanKey: 0 } }
    )

    await waitFor(() => expect(roamEnvironment).toHaveBeenCalled())
    const callsBeforeRelease = roamEnvironment.mock.calls.length

    rerender({ replanKey: 1 })
    await waitFor(() => expect(roamEnvironment.mock.calls.length).toBeGreaterThan(callsBeforeRelease))

    unmount()
  })
})
