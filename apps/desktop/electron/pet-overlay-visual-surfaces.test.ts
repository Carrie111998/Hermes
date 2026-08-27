import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const electronMock = vi.hoisted(() => {
  const width = 320
  const height = 180
  const data = new Uint8Array(width * height * 4)

  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const value = y >= 92 ? 210 : y >= 32 ? 130 : 45
      const offset = (y * width + x) * 4

      data[offset] = value
      data[offset + 1] = value
      data[offset + 2] = value
      data[offset + 3] = 255
    }
  }

  const image = {
    getSize: () => ({ height, width }),
    isEmpty: () => false,
    resize: vi.fn(() => image),
    toBitmap: vi.fn(() => data)
  }

  return {
    desktopCapturer: {
      getSources: vi.fn(async () => [{ display_id: '1', thumbnail: image }])
    },
    display: {
      bounds: { height, width, x: 0, y: 0 },
      id: 1,
      workArea: { height, width, x: 0, y: 0 }
    },
    image
  }
})

vi.mock('electron', () => ({
  desktopCapturer: electronMock.desktopCapturer,
  screen: {
    getDisplayMatching: () => electronMock.display
  }
}))

import { captureVisualLedgeBelow, detectPetOverlayVisualLedges } from './pet-overlay-visual-surfaces'

const grayscaleFrame = (width: number, height: number, pixel: (x: number, y: number) => number) => {
  const bitmap = new Uint8Array(width * height * 4)

  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const value = pixel(x, y)
      const offset = (y * width + x) * 4

      bitmap[offset] = value
      bitmap[offset + 1] = value
      bitmap[offset + 2] = value
      bitmap[offset + 3] = 255
    }
  }

  return { bitmap, displayBounds: { height, width, x: 0, y: 0 }, height, width }
}

beforeEach(() => {
  electronMock.desktopCapturer.getSources.mockClear()
  electronMock.image.resize.mockClear()
  electronMock.image.toBitmap.mockClear()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('pet overlay visual surface snapshots', () => {
  const frame = {
    bitmap: electronMock.image.toBitmap(),
    displayBounds: { ...electronMock.display.bounds },
    height: electronMock.display.bounds.height,
    width: electronMock.display.bounds.width
  }

  it('analyzes a narrow moving probe without depending on the host platform', () => {
    const overlayBounds = { height: 80, width: 80, x: 150, y: 0 }

    const supportOnly = detectPetOverlayVisualLedges(frame, {
      motionProbe: true,
      overlayBounds,
      petWidth: 64,
      scanMode: 'support',
      workArea: electronMock.display.workArea
    })

    const landing = detectPetOverlayVisualLedges(frame, {
      motionProbe: true,
      overlayBounds,
      petWidth: 64,
      scanMode: 'landing',
      workArea: electronMock.display.workArea
    })

    expect(supportOnly).toEqual([])
    expect(landing).toMatchObject([{ y: 92 }])
    expect(landing[0]!.right - landing[0]!.left).toBe(192)
  })

  it('finds the nearest vertical hop destination before tracing its full span', () => {
    const destinations = detectPetOverlayVisualLedges(frame, {
      overlayBounds: { height: 80, width: 80, x: 120, y: 0 },
      petHeight: 32,
      petWidth: 64,
      scanMode: 'destination',
      workArea: electronMock.display.workArea
    })

    expect(destinations).toMatchObject([{ y: 32 }])
    expect(destinations[0]!.right - destinations[0]!.left).toBe(320)
  })

  it('traces a selected contrast support sideways until the visible edge ends', () => {
    const boundedFrame = grayscaleFrame(320, 180, (x, y) => (x >= 40 && x < 280 && y >= 32 ? 130 : 45))

    const destinations = detectPetOverlayVisualLedges(boundedFrame, {
      // Keep the pet well off-center so a full-width trace must shift its scan
      // window to the display edge instead of truncating the opposite side.
      overlayBounds: { height: 80, width: 80, x: 200, y: 40 },
      petHeight: 32,
      petWidth: 64,
      scanMode: 'destination',
      workArea: electronMock.display.workArea
    })

    expect(destinations).toMatchObject([{ left: 40, right: 280, y: 32 }])
  })
})

describe.skipIf(process.platform !== 'win32')('pet overlay Windows capture cache', () => {
  it('reuses a recent frame and refreshes it after the requested maximum age', async () => {
    let now = 1000
    const bounds = { height: 80, width: 80, x: 120, y: 0 }
    const overlay = { getBounds: () => bounds }

    vi.spyOn(Date, 'now').mockImplementation(() => now)

    await expect(captureVisualLedgeBelow(overlay as never, 64)).resolves.toMatchObject({
      ledges: [{ y: 92 }]
    })

    now += 500

    await expect(captureVisualLedgeBelow(overlay as never, 64, true, 'landing', undefined, 750)).resolves.toMatchObject(
      {
        ledges: [{ y: 92 }]
      }
    )

    expect(electronMock.desktopCapturer.getSources).toHaveBeenCalledTimes(1)

    now += 751

    await captureVisualLedgeBelow(overlay as never, 64, true, 'landing', undefined, 750)

    expect(electronMock.desktopCapturer.getSources).toHaveBeenCalledTimes(2)
  })
})
