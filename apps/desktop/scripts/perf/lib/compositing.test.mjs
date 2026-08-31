import assert from 'node:assert/strict'

import { test } from 'vitest'

import { aggregateTraceEvents } from './compositing.mjs'

const complete = (name, durUs) => ({ name, ph: 'X', dur: durUs, ts: 0 })

test('attributes a phase as a share of the trace window', () => {
  // 6871ms of Layerize in a 15s window is the real measurement this metric
  // exists to catch (45.8% of wall clock on the main thread).
  const phases = aggregateTraceEvents([complete('Layerize', 6_871_000)], 15_000)

  assert.equal(phases.Layerize.pct, 45.8)
  assert.equal(phases.Layerize.ms, 6871)
  assert.equal(phases.Layerize.n, 1)
})

test('sums repeated events and keeps the worst single pass', () => {
  const phases = aggregateTraceEvents(
    [complete('Layerize', 4000), complete('Layerize', 10_000), complete('Layerize', 6000)],
    1000
  )

  assert.equal(phases.Layerize.n, 3)
  assert.equal(phases.Layerize.ms, 20)
  assert.equal(phases.Layerize.max_ms, 10)
})

test('reports every tracked phase, including ones the trace never emitted', () => {
  // A scenario that never painted must still publish paint_pct: 0, otherwise
  // the baseline gate sees a missing key instead of a real zero.
  const phases = aggregateTraceEvents([complete('Layerize', 1000)], 1000)

  assert.equal(phases.Paint.pct, 0)
  assert.equal(phases.Paint.n, 0)
  assert.equal(phases.Commit.ms, 0)
})

test('ignores instant and async events that carry no duration', () => {
  // Begin/End pairs and instant marks share the stream; counting them as
  // complete events would inflate every phase.
  const phases = aggregateTraceEvents(
    [
      { name: 'Layerize', ph: 'B', ts: 0 },
      { name: 'Layerize', ph: 'E', ts: 5000 },
      { name: 'Layerize', ph: 'I', ts: 1 },
      complete('Layerize', 2000)
    ],
    1000
  )

  assert.equal(phases.Layerize.n, 1)
  assert.equal(phases.Layerize.ms, 2)
})

test('survives a zero-length window without dividing by zero', () => {
  const phases = aggregateTraceEvents([complete('Layerize', 1000)], 0)

  assert.ok(Number.isFinite(phases.Layerize.pct))
})

test('ignores untracked phases so the metric set stays stable', () => {
  const phases = aggregateTraceEvents([complete('SomeFutureBlinkPhase', 9_000_000)], 1000)

  assert.equal(phases.Layerize.ms, 0)
  assert.equal(Object.hasOwn(phases, 'SomeFutureBlinkPhase'), false)
})
