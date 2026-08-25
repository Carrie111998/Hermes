import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  ANNOTATION_TTL_DEFAULT_S,
  ANNOTATION_TTL_MAX_S,
  ANNOTATION_TTL_MIN_S,
  clampAnnotationTtlSeconds,
  isScreenTarget,
  mapAnnotationShapes,
  resolveAnnotationWindow
} from './screen-annotations'
import type { EnumeratedWindow } from './window-below'

const win = (overrides: Partial<EnumeratedWindow>): EnumeratedWindow => ({
  app: 'App',
  bounds: { x: 0, y: 0, width: 800, height: 600 },
  id: 1,
  pid: 100,
  title: '',
  ...overrides
})

const SELF_PID = 42

// ── TTL ──────────────────────────────────────────────────────────────────────

test('ttl defaults when absent or garbage, and clamps both ends', () => {
  assert.equal(clampAnnotationTtlSeconds(undefined), ANNOTATION_TTL_DEFAULT_S)
  assert.equal(clampAnnotationTtlSeconds('soon'), ANNOTATION_TTL_DEFAULT_S)
  assert.equal(clampAnnotationTtlSeconds(Number.NaN), ANNOTATION_TTL_DEFAULT_S)
  assert.equal(clampAnnotationTtlSeconds(0.5), ANNOTATION_TTL_MIN_S)
  assert.equal(clampAnnotationTtlSeconds(86_400), ANNOTATION_TTL_MAX_S)
  assert.equal(clampAnnotationTtlSeconds(45), 45)
})

// ── Target spec ──────────────────────────────────────────────────────────────

test('screen target accepts screen/display in any casing, nothing else', () => {
  assert.equal(isScreenTarget('screen'), true)
  assert.equal(isScreenTarget(' Display '), true)
  assert.equal(isScreenTarget('Chess'), false)
  assert.equal(isScreenTarget(undefined), false)
  assert.equal(isScreenTarget(''), false)
})

// ── Window resolution ────────────────────────────────────────────────────────

test('a named target takes the first front-to-back app or title match, case-insensitively', () => {
  const windows = [
    win({ app: 'Hermes', pid: SELF_PID }),
    win({ app: 'Stockfish GUI', id: 7, title: 'Analysis' }),
    win({ app: 'Chess', id: 8 }),
    win({ app: 'Chess', id: 9 })
  ]

  assert.equal(resolveAnnotationWindow(windows, SELF_PID, null, 'chess').window?.id, 8)
  assert.equal(resolveAnnotationWindow(windows, SELF_PID, null, 'analysis').window?.id, 7)
})

test('a named target never matches our own windows or zero-area rows', () => {
  const windows = [
    win({ app: 'Chess Helper', pid: SELF_PID, id: 1 }),
    win({ app: 'Chess', id: 2, bounds: { x: 0, y: 0, width: 0, height: 0 } }),
    win({ app: 'Chess', id: 3 })
  ]

  assert.equal(resolveAnnotationWindow(windows, SELF_PID, null, 'chess').window?.id, 3)
})

test('an unmatched target reports the visible apps and the screen escape hatch', () => {
  const windows = [win({ app: 'Finder', id: 2 }), win({ app: 'Terminal', id: 3 })]
  const resolved = resolveAnnotationWindow(windows, SELF_PID, null, 'Chess')

  assert.equal(resolved.window, undefined)
  assert.match(resolved.error ?? '', /No window matching "Chess"/)
  assert.match(resolved.error ?? '', /Finder, Terminal/)
  assert.match(resolved.error ?? '', /target='screen'/)
})

test('no target anchors to the window below the asking Hermes window', () => {
  const selfBounds = { x: 100, y: 100, width: 400, height: 200 }

  const windows = [
    win({ app: 'Front App', id: 5, bounds: { x: 2000, y: 0, width: 800, height: 600 } }),
    win({ app: 'Hermes', pid: SELF_PID, bounds: selfBounds }),
    win({ app: 'Chess', id: 6, bounds: { x: 0, y: 0, width: 1440, height: 900 } })
  ]

  // Front App does not overlap the HUD, so the overlapping window BEHIND the
  // HUD wins — same answer read_window_below gives.
  assert.equal(resolveAnnotationWindow(windows, SELF_PID, selfBounds, undefined).window?.id, 6)
})

test('no target falls back to the frontmost other-process window when nothing overlaps', () => {
  const selfBounds = { x: 100, y: 100, width: 400, height: 200 }

  const windows = [
    win({ app: 'Hermes', pid: SELF_PID, bounds: selfBounds }),
    win({ app: 'Chess', id: 6, bounds: { x: 3000, y: 0, width: 800, height: 600 } })
  ]

  assert.equal(resolveAnnotationWindow(windows, SELF_PID, selfBounds, undefined).window?.id, 6)
})

test('no other windows at all is an actionable error, not a crash', () => {
  const resolved = resolveAnnotationWindow([win({ app: 'Hermes', pid: SELF_PID })], SELF_PID, null, undefined)

  assert.equal(resolved.window, undefined)
  assert.match(resolved.error ?? '', /target='screen'/)
})

// ── Shape mapping ────────────────────────────────────────────────────────────

const DISPLAY = { x: 0, y: 0, width: 1728, height: 1117 }

test('frame pixels map onto the live window bounds — the Retina 2x case', () => {
  // A 1024x640 window at (100, 50), captured at 2x: the screenshot is
  // 2048x1280. A circle centered in the screenshot must land centered in the
  // window, with its radius halved.
  const target = { x: 100, y: 50, width: 1024, height: 640 }
  const frame = { width: 2048, height: 1280 }

  const { shapes, skipped } = mapAnnotationShapes(
    [{ kind: 'circle', radius: 100, x: 1024, y: 640 }],
    frame,
    target,
    DISPLAY
  )

  assert.equal(skipped, 0)
  assert.deepEqual(shapes, [{ color: 'red', kind: 'circle', label: undefined, radius: 50, x: 612, y: 370 }])
})

test('overlay-local coordinates subtract the display origin', () => {
  const display = { x: -1920, y: 0, width: 1920, height: 1080 }
  const target = { x: -1720, y: 100, width: 400, height: 300 }

  const { shapes } = mapAnnotationShapes(
    [{ kind: 'label', text: 'here', x: 0, y: 0 }],
    { width: 400, height: 300 },
    target,
    display
  )

  assert.deepEqual(shapes, [{ color: 'red', kind: 'label', text: 'here', x: 200, y: 100 }])
})

test('arrows map both endpoints and rects scale their size', () => {
  const target = { x: 0, y: 0, width: 500, height: 500 }
  const frame = { width: 1000, height: 1000 }

  const { shapes } = mapAnnotationShapes(
    [
      { from_x: 100, from_y: 100, kind: 'arrow', to_x: 900, to_y: 900 },
      { height: 200, kind: 'rect', width: 400, x: 200, y: 600 }
    ],
    frame,
    target,
    DISPLAY
  )

  assert.deepEqual(shapes[0], {
    color: 'red',
    fromX: 50,
    fromY: 50,
    kind: 'arrow',
    label: undefined,
    toX: 450,
    toY: 450
  })
  assert.deepEqual(shapes[1], { color: 'red', height: 100, kind: 'rect', label: undefined, width: 200, x: 100, y: 300 })
})

test('an omitted radius gets a visible default and a tiny one is floored', () => {
  const target = { x: 0, y: 0, width: 1000, height: 1000 }
  const frame = { width: 1000, height: 1000 }

  const { shapes } = mapAnnotationShapes(
    [
      { kind: 'circle', x: 10, y: 10 },
      { kind: 'circle', radius: 1, x: 20, y: 20 }
    ],
    frame,
    target,
    DISPLAY
  )

  assert.equal((shapes[0] as { radius: number }).radius, 36)
  assert.equal((shapes[1] as { radius: number }).radius, 12)
})

test('unknown colors fall back to red and captions are trimmed and capped', () => {
  const target = { x: 0, y: 0, width: 100, height: 100 }
  const frame = { width: 100, height: 100 }

  const { shapes } = mapAnnotationShapes(
    [{ color: 'chartreuse', kind: 'circle', label: `  ${'x'.repeat(500)}  `, x: 1, y: 1 }],
    frame,
    target,
    DISPLAY
  )

  const circle = shapes[0] as { color: string; label?: string }

  assert.equal(circle.color, 'red')
  assert.equal(circle.label?.length, 120)
})

test('undrawable entries are skipped and counted, never crash the batch', () => {
  const target = { x: 0, y: 0, width: 100, height: 100 }
  const frame = { width: 100, height: 100 }

  const { shapes, skipped } = mapAnnotationShapes(
    [
      { kind: 'circle', x: 1, y: 1 },
      { kind: 'scribble', x: 1, y: 1 },
      { kind: 'circle', x: 'left' },
      { kind: 'label', x: 1, y: 1 },
      null,
      'nope'
    ],
    frame,
    target,
    DISPLAY
  )

  assert.equal(shapes.length, 1)
  assert.equal(skipped, 5)
})

test('a non-array shapes payload maps to nothing rather than throwing', () => {
  const { shapes, skipped } = mapAnnotationShapes(
    { kind: 'circle' },
    { width: 100, height: 100 },
    { x: 0, y: 0, width: 100, height: 100 },
    DISPLAY
  )

  assert.equal(shapes.length, 0)
  assert.equal(skipped, 0)
})
