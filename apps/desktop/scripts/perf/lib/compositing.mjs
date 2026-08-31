// Compositing cost: the part of the rendering pipeline the harness could not
// see.
//
// Every other lib here measures JS (`withCpuProfile`), commits
// (`__RENDER_COUNTS__`) or frame pacing (rAF deltas). None of them observe the
// compositor. That is a real blind spot: `Layerize` — Blink rebuilding the
// compositing layer tree — runs ON THE MAIN THREAD, between paint and commit,
// and it is invisible to all three. A renderer can therefore report zero
// longtasks, near-zero script time, and still drop frames and stall keystrokes,
// which is exactly what the committed `multitab` baseline shows: 0 longtasks
// next to 9 slow frames and a 36ms p99.
//
// Layer promotion is not free and not bounded by anything the app declares: an
// active transform/opacity animation promotes its element, and everything that
// overlaps a composited layer and paints above it gets promoted too (the
// `Overlap` reason). A handful of small animated indicators can therefore
// multiply into a layer tree large enough that rebuilding it dominates the
// frame — and the rebuild happens again on every tree invalidation, i.e.
// continuously while content streams.
//
// Usage mirrors withCpuProfile:
//
//   const { result, compositing } = await withCompositingTrace(cdp, () => driveScenario())
//   const layers = await layerSnapshot(cdp)

import { sleep } from './cdp.mjs'

/** Timeline events worth attributing. Names are stable devtools.timeline phases. */
const TRACKED = ['Layerize', 'Commit', 'PrePaint', 'Paint', 'UpdateLayoutTree', 'Layout', 'UpdateLayer', 'HitTest']

/**
 * Fold raw trace events into per-phase totals.
 *
 * Pure so it can be unit-tested without a browser: the CDP plumbing below is
 * thin, the arithmetic is what regressions hide in. `wallMs` is the trace
 * window, used to express each phase as a share of wall clock — the only form
 * that is comparable across machines of different speeds.
 */
export function aggregateTraceEvents(events, wallMs) {
  const byName = new Map()

  for (const e of events) {
    // 'X' is a complete event; anything else has no duration to attribute.
    if (e.ph !== 'X' || typeof e.dur !== 'number') {
      continue
    }

    const cur = byName.get(e.name) || { us: 0, n: 0, maxUs: 0 }

    cur.us += e.dur
    cur.n += 1
    cur.maxUs = Math.max(cur.maxUs, e.dur)
    byName.set(e.name, cur)
  }

  const wallUs = Math.max(1, wallMs * 1000)
  const phases = {}

  for (const name of TRACKED) {
    const v = byName.get(name) || { us: 0, n: 0, maxUs: 0 }

    phases[name] = {
      ms: Math.round(v.us / 100) / 10,
      pct: Math.round((1000 * v.us) / wallUs) / 10,
      n: v.n,
      max_ms: Math.round(v.maxUs / 100) / 10
    }
  }

  return phases
}

/**
 * Run `body` with a devtools.timeline trace open and return its phase totals.
 *
 * Only `devtools.timeline` is enabled: the disabled-by-default categories add
 * hundreds of thousands of UpdateLayer events per run (173k in 15s on a loaded
 * renderer), which costs more to ship over CDP than it explains.
 */
export async function withCompositingTrace(cdp, body, { categories = ['devtools.timeline'] } = {}) {
  const events = []
  let completed

  const done = new Promise(resolve => {
    completed = resolve
  })

  cdp.on('Tracing.dataCollected', params => events.push(...params.value))
  cdp.on('Tracing.tracingComplete', () => completed())

  await cdp.send('Tracing.start', {
    transferMode: 'ReportEvents',
    traceConfig: { includedCategories: categories }
  })

  const startedAt = Date.now()
  let result

  try {
    result = await body()
  } finally {
    // Always close the trace so a scenario error can't leave tracing armed and
    // poison every later scenario in the same run.
    await cdp.send('Tracing.end')
    await done
  }

  const wallMs = Date.now() - startedAt
  const phases = aggregateTraceEvents(events, wallMs)

  return {
    result,
    compositing: {
      wall_ms: wallMs,
      layerize_pct: phases.Layerize.pct,
      layerize_ms: phases.Layerize.ms,
      layerize_n: phases.Layerize.n,
      commit_pct: phases.Commit.pct,
      recalc_pct: phases.UpdateLayoutTree.pct,
      layout_pct: phases.Layout.pct,
      paint_pct: phases.Paint.pct,
      phases
    }
  }
}

/**
 * Layer-tree shape: how many layers exist, how much surface they cover, and
 * why they were promoted.
 *
 * `layer_area_mp` is the sum of layer areas, NOT the viewport — a healthy tree
 * is a small multiple of the viewport, and a tree covering 10x the viewport is
 * the signature of runaway promotion. `reasons` is sampled: asking the backend
 * for compositing reasons costs one round trip per layer, and the histogram
 * shape is stable well before the full tree is walked.
 */
export async function layerSnapshot(cdp, { settleMs = 1200, reasonSample = 60 } = {}) {
  let layers = []

  cdp.on('LayerTree.layerTreeDidChange', params => {
    layers = params.layers || []
  })

  await cdp.send('DOM.enable')
  await cdp.send('LayerTree.enable')
  await sleep(settleMs)

  const areaPx = layers.reduce((sum, l) => sum + (l.width || 0) * (l.height || 0), 0)
  const reasons = {}

  for (const layer of layers.slice(0, reasonSample)) {
    try {
      const r = await cdp.send('LayerTree.compositingReasons', { layerId: layer.layerId })

      for (const id of r.compositingReasonIds || r.compositingReasons || []) {
        reasons[id] = (reasons[id] || 0) + 1
      }
    } catch {
      // A layer can vanish between the snapshot and the query on a live tree;
      // a missing reason must not fail the whole scenario.
    }
  }

  return {
    layer_count: layers.length,
    layer_area_mp: Math.round((areaPx / 1e6) * 10) / 10,
    reasons_sampled: Math.min(layers.length, reasonSample),
    reasons
  }
}

/**
 * Count layer-tree invalidations over a window the CALLER defines.
 *
 * Deliberately separate from `layerSnapshot`: the interesting rate is the one
 * during load, not during the quiet settle a snapshot needs. Measuring it
 * inside the snapshot reported the settle window and produced a number
 * (~3/s) that looked healthy while the real figure under streaming was two
 * orders of magnitude higher. Requires `LayerTree.enable` to be active — call
 * `layerSnapshot` first.
 *
 *   const stop = watchLayerTreeRate(cdp)
 *   ... drive the scenario ...
 *   const { layer_tree_rate } = stop()
 */
export function watchLayerTreeRate(cdp) {
  let changes = 0
  const startedAt = Date.now()

  cdp.on('LayerTree.layerTreeDidChange', () => {
    changes += 1
  })

  return () => ({
    layer_tree_rate: Math.round(changes / Math.max(0.001, (Date.now() - startedAt) / 1000)),
    layer_tree_changes: changes
  })
}

/** Stop listening to layer-tree churn once a scenario is done with it. */
export async function endLayerSnapshot(cdp) {
  try {
    await cdp.send('LayerTree.disable')
  } catch {
    // Best effort: teardown must never mask a scenario's real failure.
  }
}
