// screen-annotations.ts — pure geometry for the agent's on-screen marks.
//
// Backs the desktop-gated `annotate_screen` tool: the renderer receives
// `screen.annotate.request` from the gateway, asks main over IPC, and main
// paints the shapes on a transparent, click-through, always-on-top overlay
// window (see screen-annotations-window.ts). Everything Electron-free lives
// here so the parts that actually break a user — which window the marks anchor
// to, and where a frame-pixel coordinate lands on screen — are unit-testable
// without booting Electron.
//
// Coordinate contract: the agent passes shape coordinates in the pixel space
// of the screenshot it analyzed (`frame`), and this module maps them onto the
// target window's live bounds. The ratio between the two absorbs Retina/DPI
// scaling without the model ever knowing about it.

import { type EnumeratedWindow, pickWindowBelow } from './window-below'

export interface AnnotationBounds {
  x: number
  y: number
  width: number
  height: number
}

export interface AnnotationFrame {
  width: number
  height: number
}

export const ANNOTATION_COLORS = ['red', 'green', 'blue', 'yellow', 'orange', 'purple', 'white', 'black'] as const

export type AnnotationColor = (typeof ANNOTATION_COLORS)[number]

const DEFAULT_COLOR: AnnotationColor = 'red'

/** One mark in overlay-local coordinates (DIP, relative to the overlay
 *  window's top-left), ready for the renderer to paint verbatim.
 *
 *  `steady` opts a shape out of the entrance/pulse animation — internal
 *  callers that replace shapes rapidly (live subtitles) need text that sits
 *  still. `fill` turns a rect from an outline into an opaque cover; it is not
 *  reachable from the agent schema — covering content is the subtitle
 *  channel's job, pointing at it is the agent's. */
export type MappedAnnotationShape =
  | {
      color: AnnotationColor
      kind: 'arrow' | 'line'
      fromX: number
      fromY: number
      label?: string
      steady?: boolean
      toX: number
      toY: number
    }
  | { color: AnnotationColor; kind: 'circle'; label?: string; radius: number; steady?: boolean; x: number; y: number }
  | { color: AnnotationColor; fontSize?: number; kind: 'label'; steady?: boolean; text: string; x: number; y: number }
  | {
      color: AnnotationColor
      fill?: boolean
      height: number
      kind: 'rect'
      label?: string
      steady?: boolean
      width: number
      x: number
      y: number
    }

// Auto-expiry bounds. The default outlives a glance but not a lunch break; the
// clamp keeps a typo'd ttl from parking marks on the screen for a day.
export const ANNOTATION_TTL_DEFAULT_S = 30
export const ANNOTATION_TTL_MIN_S = 3
export const ANNOTATION_TTL_MAX_S = 300

export function clampAnnotationTtlSeconds(raw: unknown): number {
  const value = typeof raw === 'number' && Number.isFinite(raw) ? raw : ANNOTATION_TTL_DEFAULT_S

  return Math.min(ANNOTATION_TTL_MAX_S, Math.max(ANNOTATION_TTL_MIN_S, value))
}

/** Independent shape sets sharing the one overlay window. The agent's marks
 *  and the live-subtitle painter replace/expire on their own clocks; the
 *  renderer always paints the union (subtitles last, i.e. on top). */
export type AnnotationChannel = 'agent' | 'subtitles'

// Safety expiry for hold-until-replaced channels. Live subtitle lines replace
// each other every couple of seconds; if the producer dies mid-movie this is
// how long its last line survives it. Distinct from the agent TTL clamp above:
// a held channel has a live producer refreshing it, so it needs no 3s floor.
export const CHANNEL_HOLD_DEFAULT_S = 15
export const CHANNEL_HOLD_MAX_S = 60

export function clampChannelHoldSeconds(raw: unknown): number {
  const value = typeof raw === 'number' && Number.isFinite(raw) && raw > 0 ? raw : CHANNEL_HOLD_DEFAULT_S

  return Math.min(CHANNEL_HOLD_MAX_S, value)
}

/** Axis-aligned box of one overlay-local shape. Labels get a generous
 *  width guess so a tight overlay window does not clip the last glyph. */
export function annotationShapeBounds(shape: MappedAnnotationShape): AnnotationBounds | null {
  if (shape.kind === 'rect') {
    return { height: shape.height, width: shape.width, x: shape.x, y: shape.y }
  }

  if (shape.kind === 'circle') {
    return {
      height: shape.radius * 2,
      width: shape.radius * 2,
      x: shape.x - shape.radius,
      y: shape.y - shape.radius
    }
  }

  if (shape.kind === 'label') {
    const size = shape.fontSize && shape.fontSize > 0 ? shape.fontSize : 15
    const lines = shape.text.split('\n').filter(line => line.trim().length > 0)
    const longest = lines.reduce((max, line) => Math.max(max, line.length), 0)
    const width = Math.max(size, longest * size * 0.65)
    const height = size * (1 + 1.3 * Math.max(0, lines.length - 1))

    return { height, width, x: shape.x - width / 2, y: shape.y - size }
  }

  const x = Math.min(shape.fromX, shape.toX)
  const y = Math.min(shape.fromY, shape.toY)

  return {
    height: Math.max(1, Math.abs(shape.toY - shape.fromY)),
    width: Math.max(1, Math.abs(shape.toX - shape.fromX)),
    x,
    y
  }
}

export function unionAnnotationBounds(shapes: MappedAnnotationShape[]): AnnotationBounds | null {
  let left = Infinity
  let top = Infinity
  let right = -Infinity
  let bottom = -Infinity

  for (const shape of shapes) {
    const box = annotationShapeBounds(shape)

    if (!box) {
      continue
    }

    left = Math.min(left, box.x)
    top = Math.min(top, box.y)
    right = Math.max(right, box.x + box.width)
    bottom = Math.max(bottom, box.y + box.height)
  }

  if (!Number.isFinite(left) || right - left < 1 || bottom - top < 1) {
    return null
  }

  return { height: bottom - top, width: right - left, x: left, y: top }
}

export function offsetAnnotationShapes(shapes: MappedAnnotationShape[], dx: number, dy: number): MappedAnnotationShape[] {
  return shapes.map(shape => {
    if (shape.kind === 'rect' || shape.kind === 'circle' || shape.kind === 'label') {
      return { ...shape, x: shape.x + dx, y: shape.y + dy }
    }

    return {
      ...shape,
      fromX: shape.fromX + dx,
      fromY: shape.fromY + dy,
      toX: shape.toX + dx,
      toY: shape.toY + dy
    }
  })
}

/** Screen bounds for an overlay that only needs to cover `shapes`. Agent
 *  marks can land anywhere on the display, so callers pass the full display
 *  when that channel is occupied. */
export function overlayBoundsForShapes(
  shapes: MappedAnnotationShape[],
  display: AnnotationBounds,
  pad = 12
): AnnotationBounds {
  const local = unionAnnotationBounds(shapes)

  if (!local) {
    return display
  }

  const x = display.x + local.x - pad
  const y = display.y + local.y - pad
  const left = Math.max(display.x, x)
  const top = Math.max(display.y, y)
  const right = Math.min(display.x + display.width, x + local.width + pad * 2)
  const bottom = Math.min(display.y + display.height, y + local.height + pad * 2)

  return {
    height: Math.max(8, bottom - top),
    width: Math.max(8, right - left),
    x: left,
    y: top
  }
}

/** `target: 'screen'` anchors coordinates to the whole display instead of a
 *  window — for coordinates read off a full-display screenshot. */
export const isScreenTarget = (spec: string | undefined): boolean => {
  const value = (spec ?? '').trim().toLowerCase()

  return value === 'screen' || value === 'display'
}

export type AnnotationWindowResolution =
  { error: string; window?: undefined } | { error?: undefined; window: EnumeratedWindow }

const hasArea = (win: EnumeratedWindow): boolean => win.bounds.width > 0 && win.bounds.height > 0

/**
 * Pick the window the marks anchor to from a front-to-back enumeration.
 *
 * A named target takes the FIRST front-to-back window whose app or title
 * contains it (case-insensitively) — front-to-back so "Chess" means the chess
 * window the user can see, not a buried one. No target means the window
 * directly behind the Hermes window that asked, resolved by the same
 * `pickWindowBelow` the read_window_below tool uses so the two can never
 * disagree; its frontmost fallback covers a Hermes window parked on another
 * display. Zero-area rows (minimized windows on some platforms) never match —
 * marks anchored to one would land nowhere.
 */
export function resolveAnnotationWindow(
  windows: EnumeratedWindow[],
  selfPid: number,
  selfBounds: AnnotationBounds | null,
  spec: string | undefined
): AnnotationWindowResolution {
  const named = (spec ?? '').trim().toLowerCase()

  if (named) {
    const match = windows.find(
      win =>
        win.pid !== selfPid &&
        hasArea(win) &&
        (win.app.toLowerCase().includes(named) || win.title.toLowerCase().includes(named))
    )

    if (match) {
      return { window: match }
    }

    const visible = [
      ...new Set(windows.filter(win => win.pid !== selfPid && hasArea(win) && win.app).map(win => win.app))
    ]

    const listing = visible.slice(0, 8).join(', ')

    return {
      error:
        `No window matching "${spec?.trim()}" is on screen.` +
        (listing ? ` Visible apps: ${listing}.` : ' No other windows are visible.') +
        " Pass target='screen' to draw in whole-display coordinates instead."
    }
  }

  const { below, frontmost } = pickWindowBelow(windows, selfPid, selfBounds ?? { x: 0, y: 0, width: 0, height: 0 })
  const candidate = [below, frontmost, ...windows.filter(win => win.pid !== selfPid)].find(win => win && hasArea(win))

  if (candidate) {
    return { window: candidate }
  }

  return {
    error:
      'No other window is on screen to anchor the marks to. ' +
      "Pass target='screen' to draw in whole-display coordinates instead."
  }
}

interface RawShape {
  color?: unknown
  font_size?: unknown
  from_x?: unknown
  from_y?: unknown
  height?: unknown
  kind?: unknown
  label?: unknown
  radius?: unknown
  text?: unknown
  to_x?: unknown
  to_y?: unknown
  width?: unknown
  x?: unknown
  y?: unknown
}

const asNumber = (value: unknown): number | null => (typeof value === 'number' && Number.isFinite(value) ? value : null)

const asColor = (value: unknown): AnnotationColor =>
  typeof value === 'string' && (ANNOTATION_COLORS as readonly string[]).includes(value)
    ? (value as AnnotationColor)
    : DEFAULT_COLOR

// Captions are drawn on the screen, not read aloud — a paragraph would cover
// the very thing the mark points at.
const MAX_TEXT_CHARS = 120

const asCaption = (value: unknown): string | undefined => {
  const text = typeof value === 'string' ? value.trim() : ''

  return text ? text.slice(0, MAX_TEXT_CHARS) : undefined
}

// Visibility floors, in DIP. A sub-pixel circle or rect is a draw that
// happened but cannot be seen — worse than an error.
const MIN_CIRCLE_RADIUS = 12
const DEFAULT_CIRCLE_RADIUS = 36
const MIN_RECT_SIZE = 8

// Label font-size bounds, in DIP after frame→screen scaling. The floor keeps a
// tiny-frame coordinate space from producing unreadable text; the ceiling keeps
// a typo (font_size in thousandths, say) from filling the display.
const MIN_LABEL_FONT_SIZE = 10
const MAX_LABEL_FONT_SIZE = 120

export interface MappedAnnotations {
  shapes: MappedAnnotationShape[]
  /** Entries that were not drawable (bad kind, missing coordinates). The
   *  Python schema validates first, so these are IPC-boundary belt only. */
  skipped: number
}

/**
 * Frame-pixel shapes → overlay-local shapes.
 *
 * `target` is where the frame's content lives on screen (a window's bounds, or
 * the display's own bounds for target='screen'); `display` is the display the
 * overlay window covers. Scale is per-axis — the frame and the live window are
 * the same content, so their aspect ratios agree up to title-bar/shadow slop,
 * and per-axis mapping keeps edge coordinates pinned to edges even then.
 */
export function mapAnnotationShapes(
  rawShapes: unknown,
  frame: AnnotationFrame,
  target: AnnotationBounds,
  display: AnnotationBounds
): MappedAnnotations {
  const shapes: MappedAnnotationShape[] = []
  let skipped = 0

  const scaleX = target.width / frame.width
  const scaleY = target.height / frame.height
  const localX = (x: number) => Math.round(target.x + x * scaleX - display.x)
  const localY = (y: number) => Math.round(target.y + y * scaleY - display.y)

  for (const raw of Array.isArray(rawShapes) ? rawShapes : []) {
    const shape = (raw ?? {}) as RawShape
    const color = asColor(shape.color)
    const label = asCaption(shape.label)

    if (shape.kind === 'circle') {
      const x = asNumber(shape.x)
      const y = asNumber(shape.y)

      if (x === null || y === null) {
        skipped += 1

        continue
      }

      const radius = asNumber(shape.radius)

      shapes.push({
        color,
        kind: 'circle',
        label,
        radius:
          radius === null
            ? DEFAULT_CIRCLE_RADIUS
            : Math.max(MIN_CIRCLE_RADIUS, Math.round(radius * Math.min(scaleX, scaleY))),
        x: localX(x),
        y: localY(y)
      })

      continue
    }

    if (shape.kind === 'rect') {
      const x = asNumber(shape.x)
      const y = asNumber(shape.y)
      const width = asNumber(shape.width)
      const height = asNumber(shape.height)

      if (x === null || y === null || width === null || height === null) {
        skipped += 1

        continue
      }

      shapes.push({
        color,
        height: Math.max(MIN_RECT_SIZE, Math.round(height * scaleY)),
        kind: 'rect',
        label,
        width: Math.max(MIN_RECT_SIZE, Math.round(width * scaleX)),
        x: localX(x),
        y: localY(y)
      })

      continue
    }

    if (shape.kind === 'arrow' || shape.kind === 'line') {
      const fromX = asNumber(shape.from_x)
      const fromY = asNumber(shape.from_y)
      const toX = asNumber(shape.to_x)
      const toY = asNumber(shape.to_y)

      if (fromX === null || fromY === null || toX === null || toY === null) {
        skipped += 1

        continue
      }

      shapes.push({
        color,
        fromX: localX(fromX),
        fromY: localY(fromY),
        kind: shape.kind,
        label,
        toX: localX(toX),
        toY: localY(toY)
      })

      continue
    }

    if (shape.kind === 'label') {
      const x = asNumber(shape.x)
      const y = asNumber(shape.y)
      const text = asCaption(shape.text)

      if (x === null || y === null || !text) {
        skipped += 1

        continue
      }

      const rawFontSize = asNumber(shape.font_size)
      const mapped: MappedAnnotationShape = { color, kind: 'label', text, x: localX(x), y: localY(y) }

      if (rawFontSize !== null && rawFontSize > 0) {
        mapped.fontSize = Math.min(
          MAX_LABEL_FONT_SIZE,
          Math.max(MIN_LABEL_FONT_SIZE, Math.round(rawFontSize * Math.min(scaleX, scaleY)))
        )
      }

      shapes.push(mapped)

      continue
    }

    skipped += 1
  }

  return { shapes, skipped }
}
