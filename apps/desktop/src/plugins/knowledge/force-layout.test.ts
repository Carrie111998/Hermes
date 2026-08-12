import { describe, expect, it } from 'vitest'

import { forceLayout, nodeRadius } from './force-layout'

describe('forceLayout', () => {
  it('returns a position for every node', () => {
    const pos = forceLayout(['a', 'b', 'c'], [{ source: 'a', target: 'b' }])
    expect(pos.size).toBe(3)

    for (const id of ['a', 'b', 'c']) {
      const p = pos.get(id)!
      expect(Number.isFinite(p.x)).toBe(true)
      expect(Number.isFinite(p.y)).toBe(true)
    }
  })

  it('is deterministic for the same input', () => {
    const nodes = ['a', 'b', 'c', 'd']

    const edges = [
      { source: 'a', target: 'b' },
      { source: 'b', target: 'c' }
    ]

    const p1 = forceLayout(nodes, edges)
    const p2 = forceLayout(nodes, edges)
    expect(p1.get('a')).toEqual(p2.get('a'))
    expect(p1.get('c')).toEqual(p2.get('c'))
  })

  it('stays inside the viewport bounds', () => {
    const nodes = Array.from({ length: 12 }, (_, i) => `n${i}`)
    const edges = []

    for (let i = 1; i < 12; i += 1) {edges.push({ source: `n${i - 1}`, target: `n${i}` })}
    const pos = forceLayout(nodes, edges, { width: 300, height: 200 })

    for (const id of nodes) {
      const p = pos.get(id)!
      expect(p.x).toBeGreaterThanOrEqual(0)
      expect(p.x).toBeLessThanOrEqual(300)
      expect(p.y).toBeGreaterThanOrEqual(0)
      expect(p.y).toBeLessThanOrEqual(200)
    }
  })

  it('handles an empty graph', () => {
    expect(forceLayout([], []).size).toBe(0)
  })
})

describe('nodeRadius', () => {
  it('grows with degree but caps out', () => {
    expect(nodeRadius(0)).toBeLessThan(nodeRadius(4))
    expect(nodeRadius(100)).toBeLessThanOrEqual(nodeRadius(200))
    expect(nodeRadius(10000)).toBeLessThanOrEqual(18)
  })
})
