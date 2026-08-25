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
 *  window's top-left), ready for the renderer to paint verbatim. */
export type MappedAnnotationShape =
  | {
      color: AnnotationColor
      kind: 'arrow' | 'line'
      fromX: number
      fromY: number
      label?: string
      toX: number
      toY: number
    }
  | { color: AnnotationColor; kind: 'circle'; label?: string; radius: number; x: number; y: number }
  | { color: AnnotationColor; kind: 'label'; text: string; x: number; y: number }
  | { color: AnnotationColor; kind: 'rect'; height: number; label?: string; width: number; x: number; y: number }

// Auto-expiry bounds. The default outlives a glance but not a lunch break; the
// clamp keeps a typo'd ttl from parking marks on the screen for a day.
export const ANNOTATION_TTL_DEFAULT_S = 30
export const ANNOTATION_TTL_MIN_S = 3
export const ANNOTATION_TTL_MAX_S = 300

export function clampAnnotationTtlSeconds(raw: unknown): number {
  const value = typeof raw === 'number' && Number.isFinite(raw) ? raw : ANNOTATION_TTL_DEFAULT_S

  return Math.min(ANNOTATION_TTL_MAX_S, Math.max(ANNOTATION_TTL_MIN_S, value))
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

      shapes.push({ color, kind: 'label', text, x: localX(x), y: localY(y) })

      continue
    }

    skipped += 1
  }

  return { shapes, skipped }
}
