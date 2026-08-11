/**
 * Synchronous force-directed layout for the knowledge graph — Coulomb
 * repulsion between all node pairs + Hooke springs along edges, run for a
 * fixed iteration count so results are deterministic and testable.
 *
 * Ported from the approach used by Hermes-Studio's knowledge browser.
 */
export interface LayoutNode {
  id: string
  x: number
  y: number
  degree: number
}

export interface LayoutEdge {
  source: string
  target: string
}

export interface ForceLayoutOptions {
  width?: number
  height?: number
  iterations?: number
  /** Coulomb repulsion strength (scaled by 1/d²). */
  repulsion?: number
  /** Hooke spring strength. */
  attraction?: number
  /** Natural spring length. */
  springLength?: number
  seed?: number
}

function mulberry32(seed: number): () => number {
  let a = seed >>> 0

  return () => {
    a |= 0
    a = (a + 0x6d2b79f5) | 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t

    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

/**
 * Compute 2-D positions for the node set. Returns a Map<id, {x,y}> with
 * positions bounded to the requested viewport.
 */
export function forceLayout(
  nodeIds: string[],
  edges: LayoutEdge[],
  opts: ForceLayoutOptions = {}
): Map<string, { x: number; y: number }> {
  const {
    width = 800,
    height = 560,
    iterations = 280,
    repulsion = 1_800,
    attraction = 0.045,
    springLength = 110,
    seed = 42
  } = opts

  const rng = mulberry32(seed)
  const degree = new Map<string, number>()

  for (const id of nodeIds) {degree.set(id, 0)}

  for (const e of edges) {
    degree.set(e.source, (degree.get(e.source) ?? 0) + 1)
    degree.set(e.target, (degree.get(e.target) ?? 0) + 1)
  }

  // Deterministic initial placement: ring around the centre, hubs inward.
  const pos = new Map<string, { x: number; y: number }>()
  const n = nodeIds.length
  const cx = width / 2
  const cy = height / 2
  nodeIds.forEach((id, i) => {
    const angle = (i / Math.max(1, n)) * Math.PI * 2 + rng() * 0.15
    const radius = Math.min(width, height) * 0.3 + rng() * 40
    pos.set(id, { x: cx + Math.cos(angle) * radius, y: cy + Math.sin(angle) * radius })
  })

  const adjacency = new Map<string, string[]>()

  for (const id of nodeIds) {adjacency.set(id, [])}

  for (const e of edges) {
    adjacency.get(e.source)?.push(e.target)
    adjacency.get(e.target)?.push(e.source)
  }

  const displacement = new Map<string, { x: number; y: number }>()

  for (let iter = 0; iter < iterations; iter += 1) {
    for (const id of nodeIds) {displacement.set(id, { x: 0, y: 0 })}
    const cooling = 1 - iter / iterations

    // Repulsion — all pairs.
    for (let i = 0; i < n; i += 1) {
      const a = nodeIds[i]!
      const pa = pos.get(a)!

      for (let j = i + 1; j < n; j += 1) {
        const b = nodeIds[j]!
        const pb = pos.get(b)!
        const dx = pa.x - pb.x
        const dy = pa.y - pb.y
        const distSq = Math.max(dx * dx + dy * dy, 1)
        const dist = Math.sqrt(distSq)
        const force = (repulsion / distSq) * cooling
        const fx = (dx / dist) * force
        const fy = (dy / dist) * force
        const da = displacement.get(a)!
        const db = displacement.get(b)!
        da.x += fx
        da.y += fy
        db.x -= fx
        db.y -= fy
      }
    }

    // Attraction — springs along edges.
    for (const e of edges) {
      const pa = pos.get(e.source)
      const pb = pos.get(e.target)

      if (!pa || !pb) {continue}
      const dx = pb.x - pa.x
      const dy = pb.y - pa.y
      const dist = Math.max(Math.sqrt(dx * dx + dy * dy), 1)
      const force = (dist - springLength) * attraction
      const fx = (dx / dist) * force
      const fy = (dy / dist) * force
      const da = displacement.get(e.source)!
      const db = displacement.get(e.target)!
      da.x += fx
      da.y += fy
      db.x -= fx
      db.y -= fy
    }

    // Apply, clamping to the viewport.
    for (const id of nodeIds) {
      const p = pos.get(id)!
      const d = displacement.get(id)!
      p.x += Math.max(-24, Math.min(24, d.x))
      p.y += Math.max(-24, Math.min(24, d.y))
      p.x = Math.max(24, Math.min(width - 24, p.x))
      p.y = Math.max(24, Math.min(height - 24, p.y))
    }
  }

  return pos
}

/** Node radius by degree: hubs larger, orphans small. */
export function nodeRadius(degree: number): number {
  return 5 + Math.min(13, Math.sqrt(degree) * 4)
}
