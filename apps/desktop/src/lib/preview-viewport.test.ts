import { describe, expect, it } from 'vitest'

import {
  clampEdge,
  customViewport,
  fitScale,
  MAX_EDGE,
  MIN_EDGE,
  parseEdge,
  rotateViewport,
  toWidgetPoint,
  VIEWPORT_PRESETS,
  viewportFit,
  viewportLabel
} from './preview-viewport'

const DESKTOP = { height: 900, width: 1440 }
const PHONE = { height: 844, width: 390 }

describe('presets', () => {
  it('marks phones and tablets as mobile and desktops as not', () => {
    // Measured: without mobile emulation a 390px viewport is a narrow desktop
    // window that ignores <meta viewport>, which makes a phone preset a lie.
    const byId = Object.fromEntries(VIEWPORT_PRESETS.map(preset => [preset.id, preset]))
    expect(byId.phone.mobile).toBe(true)
    expect(byId.tablet.mobile).toBe(true)
    expect(byId.desktop.mobile).toBe(false)
    expect(byId.wide.mobile).toBe(false)
  })

  it('gives every preset a distinct id', () => {
    expect(new Set(VIEWPORT_PRESETS.map(preset => preset.id)).size).toBe(VIEWPORT_PRESETS.length)
  })
})

describe('fitScale', () => {
  it('shrinks a desktop to fit a narrow rail — the whole point of the feature', () => {
    // 500px of pane, 1440px of page.
    expect(fitScale(DESKTOP, { height: 900, width: 500 })).toBeCloseTo(0.347, 2)
  })

  it('never magnifies a page that already fits', () => {
    // A phone shown at 230% would misrepresent every dimension being judged.
    expect(fitScale(PHONE, { height: 1200, width: 900 })).toBe(1)
  })

  it('binds on whichever edge is tighter', () => {
    expect(fitScale(DESKTOP, { height: 200, width: 5000 })).toBeCloseTo(200 / 900, 2)
  })

  it('survives a pane that has not been measured yet', () => {
    expect(fitScale(DESKTOP, { height: 0, width: 0 })).toBe(1)
    expect(fitScale(DESKTOP, { height: Number.NaN, width: 500 })).toBe(1)
  })

  it('ignores sub-pixel pane wobble, which is constant on a scaled display', () => {
    // A ResizeObserver under fractional scaling reports 500.4 one frame and
    // 500.37 the next; re-emulating the guest on each would be churn for
    // nothing visible.
    const whole = fitScale(DESKTOP, { height: 900, width: 500 })
    expect(fitScale(DESKTOP, { height: 900, width: 500.4 })).toBe(whole)
    expect(fitScale(DESKTOP, { height: 900.8, width: 500.99 })).toBe(whole)
  })
})

describe('viewportFit', () => {
  /** 1.2^1.6239 — the level found in this machine's zoom-state.json, i.e. 134%. */
  const ZOOM = 1.3445671961657126

  it('sizes the element so the emulated page fills it exactly', () => {
    // Verified in Electron: element = device × scale, with the same scale
    // passed to enableDeviceEmulation, leaves no letterboxing.
    const { frame, scale } = viewportFit(DESKTOP, { height: 900, width: 500 })
    expect(frame.width).toBe(Math.round(DESKTOP.width * scale))
    expect(frame.height).toBe(Math.round(DESKTOP.height * scale))
    expect(frame.width).toBeLessThanOrEqual(500)
  })

  it('never overflows the pane it was asked to fit, at any zoom', () => {
    // Rounding the scale up put a 820x1180 tablet at 390x561 inside a 500x560
    // pane — one pixel of overflow is a scrollbar. Found in a real guest, so
    // check every preset against a few awkward pane sizes rather than one.
    // Zoomed too: dividing the frame by the zoom must not round its way back
    // over the edge.
    for (const zoom of [undefined, ZOOM]) {
      for (const preset of VIEWPORT_PRESETS) {
        for (const pane of [
          { height: 560, width: 500 },
          { height: 337, width: 1201 },
          { height: 999, width: 333 },
          { height: 2000, width: 2000 }
        ]) {
          const { frame } = viewportFit(preset, pane, zoom)
          const where = `${preset.id} in ${pane.width}x${pane.height} @${zoom ?? 1}`

          expect(frame.width, where).toBeLessThanOrEqual(pane.width)
          expect(frame.height, where).toBeLessThanOrEqual(pane.height)
        }
      }
    }
  })

  it('leaves a fitting page at its own size', () => {
    expect(viewportFit(PHONE, { height: 1000, width: 800 })).toEqual({ frame: PHONE, scale: 1 })
  })

  // ── App zoom. The pane measures in host CSS pixels and Chromium paints the
  // emulated page in the guest's; app zoom is the ratio. Ignoring it shipped a
  // white band down the right and bottom of every device preview for anyone
  // who had pressed Ctrl+ — measured in a real guest at 134%: a 430x932 phone
  // got a 563x1219 widget for a 419x907 paint, exactly 1/1.3446 of it painted.
  describe('under app zoom', () => {
    it('shrinks the frame by the zoom, so the painted page fills it', () => {
      const pane = { height: 1000, width: 800 }
      const { frame, scale } = viewportFit(PHONE, pane, ZOOM)

      // What the guest paints, in guest pixels, is device x scale. The element
      // is in host pixels, and one host pixel is `zoom` guest pixels — so the
      // frame has to be that much smaller or the surplus never gets painted.
      expect(frame.width).toBe(Math.round((PHONE.width * scale) / ZOOM))
      expect(frame.height).toBe(Math.round((PHONE.height * scale) / ZOOM))
    })

    it('regression: the frame is NOT the unzoomed size', () => {
      // The exact bug. Before the fix both branches returned device x scale,
      // so this assertion is what tells the two apart.
      const pane = { height: 500, width: 400 }
      const zoomed = viewportFit(DESKTOP, pane, ZOOM)

      expect(zoomed.frame.width).toBeLessThan(Math.round(DESKTOP.width * zoomed.scale))
    })

    it('uses the zoom to fit MORE page, not less', () => {
      // A zoomed window has more guest pixels than host pixels, so a device
      // that had to be shrunk at 100% may fit at 1:1 when zoomed in.
      const pane = { height: 700, width: 340 }

      expect(viewportFit(PHONE, pane, ZOOM).scale).toBeGreaterThan(viewportFit(PHONE, pane).scale)
    })

    it('treats a missing or nonsense zoom as 100%', () => {
      const pane = { height: 500, width: 400 }
      const plain = viewportFit(DESKTOP, pane)

      for (const bad of [0, -2, Number.NaN, Number.POSITIVE_INFINITY]) {
        expect(viewportFit(DESKTOP, pane, bad)).toEqual(plain)
      }
    })
  })
})

describe('toWidgetPoint', () => {
  it('multiplies by the scale Chromium is about to divide by', () => {
    // Measured on Electron 40: injected input arrives in widget pixels and is
    // divided by `scale`. Sending the guest's own numbers puts the click at
    // 1/scale of where it belongs — off the page at desktop-in-a-rail scales.
    expect(toWidgetPoint({ x: 645, y: 335 }, 0.5)).toEqual({ x: 322.5, y: 167.5 })
  })

  it('is a no-op at 1:1, and for a nonsense scale', () => {
    expect(toWidgetPoint({ x: 10, y: 20 }, 1)).toEqual({ x: 10, y: 20 })
    expect(toWidgetPoint({ x: 10, y: 20 }, 0)).toEqual({ x: 10, y: 20 })
    expect(toWidgetPoint({ x: 10, y: 20 }, Number.NaN)).toEqual({ x: 10, y: 20 })
  })

  it('round-trips against the division it compensates for', () => {
    const scale = 0.347
    const guest = { x: 900, y: 400 }
    const widget = toWidgetPoint(guest, scale)
    expect(widget.x / scale).toBeCloseTo(guest.x, 6)
    expect(widget.y / scale).toBeCloseTo(guest.y, 6)
  })
})

describe('free values', () => {
  it('reads a typed number', () => {
    expect(parseEdge('375')).toBe(375)
    expect(parseEdge('  1024 ')).toBe(1024)
  })

  it('refuses what is not a number rather than guessing', () => {
    expect(parseEdge('')).toBeNull()
    expect(parseEdge('abc')).toBeNull()
    expect(parseEdge('12px')).toBeNull()
    expect(parseEdge('-30')).toBeNull()
  })

  it('clamps instead of rejecting — a big number means "very wide"', () => {
    expect(parseEdge('99999')).toBe(MAX_EDGE)
    expect(parseEdge('3')).toBe(MIN_EDGE)
    expect(clampEdge(Number.NaN)).toBe(MIN_EDGE)
  })

  it('treats a narrow custom size as a phone, since that is what was meant', () => {
    expect(customViewport(380, 800).mobile).toBe(true)
    expect(customViewport(1400, 900).mobile).toBe(false)
    expect(customViewport(380, 800, false).mobile).toBe(false)
  })

  it('labels itself by its size', () => {
    expect(customViewport(375, 812).label).toBe('375×812')
  })
})

describe('rotate', () => {
  it('swaps the edges and keeps everything else', () => {
    const landscape = rotateViewport(VIEWPORT_PRESETS[0])
    expect(landscape.width).toBe(VIEWPORT_PRESETS[0].height)
    expect(landscape.height).toBe(VIEWPORT_PRESETS[0].width)
    expect(landscape.mobile).toBe(true)
  })

  it('is its own inverse', () => {
    const there = rotateViewport(VIEWPORT_PRESETS[3])
    expect(rotateViewport(there).width).toBe(VIEWPORT_PRESETS[3].width)
    expect(rotateViewport(there).height).toBe(VIEWPORT_PRESETS[3].height)
  })
})

describe('viewportLabel', () => {
  it('says the zoom only when there is one', () => {
    const desktop = { ...DESKTOP, id: 'desktop', label: 'Desktop', mobile: false }
    expect(viewportLabel(desktop, 1)).toBe('1440×900')
    expect(viewportLabel(desktop, 0.347)).toBe('1440×900 · 35%')
  })
})
