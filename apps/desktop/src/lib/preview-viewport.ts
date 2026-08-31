/**
 * PREVIEW VIEWPORT — emulating a device inside the preview pane.
 *
 * The pane is whatever width the user dragged it to, which is the one width
 * nobody's site is designed for. This lets the page be told it is 390px wide on
 * a wide monitor, or 1440px wide inside a narrow rail, so a layout can be
 * checked where it actually breaks.
 *
 * WHY `enableDeviceEmulation` AND NOT A CSS TRANSFORM. A transform on the
 * <webview> element is simpler and needs no IPC, but real mouse input then has
 * to be mapped through that transform before it is routed into the guest, and a
 * <webview> guest is not an ordinary transformed box. Emulation is Chromium's
 * own device mode: the element is untouched, the guest is told its size, and
 * input is mapped by the same code that maps it for DevTools.
 *
 * Measured on Electron 40 rather than assumed:
 *   - the guest reports the emulated size and its media queries flip;
 *   - INJECTED input (`sendInputEvent`, the agent's `drive_preview` path)
 *     arrives in WIDGET pixels and Chromium divides it by `scale` — verified at
 *     three scales, and independent of the element's size. So a caller holding
 *     guest coordinates has to multiply by `scale` on the way in. That same
 *     division is what makes a real mouse land where it visually appears, which
 *     is why the pins engine needs nothing.
 *   - sizing the element to `device × scale` and passing the same `scale` makes
 *     the emulated page fill the element exactly, with no letterboxing.
 *   - `mobile` is what makes a phone preset mean anything: without it a 390px
 *     viewport is just a narrow desktop window that ignores <meta viewport>.
 *
 * Pure and dependency-free so the arithmetic can be tested without a browser.
 */

export interface Viewport {
  height: number
  id: string
  label: string
  /** Chromium's mobile emulation — honours `<meta viewport>`, shrink-to-fit and
   *  mobile scrollbars. False for laptop/desktop sizes, which are not phones. */
  mobile: boolean
  width: number
}

export interface ViewportFit {
  /** Size to give the <webview> element, in host pixels. */
  frame: { height: number; width: number }
  /** What to pass to enableDeviceEmulation, and what to multiply injected
   *  input coordinates by. */
  scale: number
}

/**
 * Sizes worth having, not every device ever made.
 *
 * Two phones because the gap between a small and a large phone is where most
 * responsive bugs live, one tablet, and three desktop widths — 1280 is the
 * common laptop, 1440 the common desktop, 1920 the one people forget to check.
 */
export const VIEWPORT_PRESETS: readonly Viewport[] = Object.freeze([
  { height: 844, id: 'phone', label: 'Phone', mobile: true, width: 390 },
  { height: 932, id: 'phone-large', label: 'Phone L', mobile: true, width: 430 },
  { height: 1180, id: 'tablet', label: 'Tablet', mobile: true, width: 820 },
  { height: 800, id: 'laptop', label: 'Laptop', mobile: false, width: 1280 },
  { height: 900, id: 'desktop', label: 'Desktop', mobile: false, width: 1440 },
  { height: 1080, id: 'wide', label: 'Wide', mobile: false, width: 1920 }
])

/** Narrow enough for the smallest phone anyone still tests, wide enough for a
 *  5K canvas, and bounded so a typo cannot ask Chromium for a 90000px viewport. */
export const MIN_EDGE = 200
export const MAX_EDGE = 5120

export function clampEdge(value: number): number {
  if (!Number.isFinite(value)) {return MIN_EDGE}

  return Math.min(MAX_EDGE, Math.max(MIN_EDGE, Math.round(value)))
}

/** A free-typed edge, or null if it was not a number at all. Out-of-range is
 *  clamped rather than rejected: the user meant "very wide", not nothing. */
export function parseEdge(text: string): null | number {
  const digits = String(text ?? '').trim()

  if (!/^\d+$/.test(digits)) {return null}

  return clampEdge(Number(digits))
}

/** Anything the user typed themselves, as a viewport. */
export function customViewport(width: number, height: number, mobile?: boolean): Viewport {
  const w = clampEdge(width)
  const h = clampEdge(height)

  return {
    height: h,
    id: 'custom',
    label: `${w}×${h}`,
    // Below the usual tablet breakpoint, treat it as a phone unless told
    // otherwise — someone typing 380 wants phone behaviour, not a tiny desktop.
    mobile: mobile ?? w < 768,
    width: w
  }
}

export function rotateViewport(viewport: Viewport): Viewport {
  return { ...viewport, height: viewport.width, label: `${viewport.height}×${viewport.width}`, width: viewport.height }
}

/**
 * How much to shrink the emulated page so it fits the space there is.
 *
 * Never above 1: a 390px phone inside a 900px pane is shown at life size and
 * centred, not stretched to fill. Blowing it up would misrepresent every
 * dimension the user is there to judge.
 */
export function fitScale(viewport: { height: number; width: number }, available: { height: number; width: number }): number {
  // Floored, not used raw: a ResizeObserver on a fractionally-scaled display
  // reports widths like 500.4 that drift by hundredths between frames, and
  // every drift would re-emulate the guest. Whole pixels are the real signal.
  const width = Number.isFinite(available.width) ? Math.floor(available.width) : 0
  const height = Number.isFinite(available.height) ? Math.floor(available.height) : 0

  if (width <= 0 || height <= 0) {return 1}

  const scale = Math.min(1, width / Math.max(1, viewport.width), height / Math.max(1, viewport.height))

  // Floored, never rounded: rounding UP produced a frame one pixel taller than
  // the pane (a 820x1180 tablet in 500x560 gave 390x561), which is a scrollbar
  // on a preview that is supposed to fit. Caught in a real Electron guest.
  return Math.max(0.05, Math.floor(scale * 1000) / 1000)
}

/**
 * The element size that makes the emulated page fill it exactly.
 *
 * `zoom` is the HOST window's zoom factor. The pane measures itself in host CSS
 * pixels, but Chromium paints the emulated page in the GUEST's CSS pixels, and
 * app zoom is exactly the ratio between the two: at 134% the guest widget came
 * out 1.34x larger
 * than the page painted into it, so a quarter of the frame stayed unpainted —
 * a white band down the right and along the bottom. Measured in a real guest:
 * a 430x932 phone reported a 563x1219 widget for a 419x907 paint.
 *
 * So: pick the scale against the space expressed in GUEST pixels, then hand the
 * element back a size in HOST pixels by dividing the zoom out again.
 */
export function viewportFit(
  viewport: { height: number; width: number },
  available: { height: number; width: number },
  zoom = 1
): ViewportFit {
  const z = Number.isFinite(zoom) && zoom > 0 ? zoom : 1
  const scale = fitScale(viewport, { height: available.height * z, width: available.width * z })

  return {
    frame: {
      height: Math.round((viewport.height * scale) / z),
      width: Math.round((viewport.width * scale) / z)
    },
    scale
  }
}

/** What the pane shows beside the size: "1440×900 · 35%", or nothing at 1:1. */
export function viewportLabel(viewport: Viewport, scale: number): string {
  const size = `${viewport.width}×${viewport.height}`

  return scale >= 0.999 ? size : `${size} · ${Math.round(scale * 100)}%`
}

/**
 * Guest coordinates as the widget wants them.
 *
 * The agent reads a rect inside the page and sends a click at it. Under
 * emulation Chromium divides incoming coordinates by `scale`, so handing it the
 * guest's own numbers lands the click at 1/scale of where it belongs — off the
 * page entirely at desktop-in-a-narrow-rail scales.
 */
export function toWidgetPoint(point: { x: number; y: number }, scale: number): { x: number; y: number } {
  if (!Number.isFinite(scale) || scale <= 0 || scale === 1) {return point}

  return { x: point.x * scale, y: point.y * scale }
}
