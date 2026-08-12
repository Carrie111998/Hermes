import { describe, expect, it } from 'vitest'

import { autoLayout, blockedTaskIds, edgeExists, hasCycle, topoLayers } from './dag'

const ids = ['a', 'b', 'c', 'd', 'e']

describe('topoLayers', () => {
  it('layers parallel tasks together (Kahn)', () => {
    const layers = topoLayers(ids, [
      { from: 'a', to: 'c' },
      { from: 'b', to: 'c' },
      { from: 'c', to: 'd' }
    ])

    // e is disconnected, so it joins the root layer with a and b.
    expect(new Set(layers[0])).toEqual(new Set(['a', 'b', 'e']))
    expect(layers[1]).toEqual(['c'])
    expect(layers[2]).toEqual(['d'])
    expect(layers.flat().length).toBe(5)
  })

  it('returns a single layer for disconnected tasks', () => {
    const layers = topoLayers(['x', 'y'], [])
    expect(layers).toHaveLength(1)
    expect(new Set(layers[0])).toEqual(new Set(['x', 'y']))
  })

  it('chain lays out one task per layer', () => {
    const layers = topoLayers(
      ['p', 'q', 'r'],
      [
        { from: 'p', to: 'q' },
        { from: 'q', to: 'r' }
      ]
    )

    expect(layers).toEqual([['p'], ['q'], ['r']])
  })
})

describe('hasCycle', () => {
  it('detects a direct cycle', () => {
    expect(
      hasCycle(
        ['a', 'b'],
        [
          { from: 'a', to: 'b' },
          { from: 'b', to: 'a' }
        ]
      )
    ).toBe(true)
  })

  it('detects an indirect cycle', () => {
    expect(
      hasCycle(
        ['a', 'b', 'c'],
        [
          { from: 'a', to: 'b' },
          { from: 'b', to: 'c' },
          { from: 'c', to: 'a' }
        ]
      )
    ).toBe(true)
  })

  it('accepts a DAG', () => {
    expect(
      hasCycle(
        ['a', 'b', 'c'],
        [
          { from: 'a', to: 'b' },
          { from: 'a', to: 'c' }
        ]
      )
    ).toBe(false)
  })

  it('treats a self-loop as a cycle', () => {
    expect(hasCycle(['a'], [{ from: 'a', to: 'a' }])).toBe(true)
  })
})

describe('edgeExists', () => {
  it('finds an existing edge', () => {
    expect(edgeExists([{ from: 'a', to: 'b' }], 'a', 'b')).toBe(true)
  })

  it('does not treat the reverse direction as a duplicate', () => {
    expect(edgeExists([{ from: 'a', to: 'b' }], 'b', 'a')).toBe(false)
  })
})

describe('autoLayout', () => {
  it('assigns x/y columns by layer and centers rows', () => {
    const tasks = [
      { id: 'a', label: '', prompt: '', assigneeId: null, x: 0, y: 0 },
      { id: 'b', label: '', prompt: '', assigneeId: null, x: 0, y: 0 },
      { id: 'c', label: '', prompt: '', assigneeId: null, x: 0, y: 0 }
    ]

    const laid = autoLayout(tasks, [
      { from: 'a', to: 'c' },
      { from: 'b', to: 'c' }
    ])

    const byId = new Map(laid.map(t => [t.id, t]))
    // a and b share a column; c is in the next column.
    expect(byId.get('a')!.x).toBeCloseTo(byId.get('b')!.x, 5)
    expect(byId.get('c')!.x).toBeGreaterThan(byId.get('a')!.x)
  })
})

describe('blockedTaskIds', () => {
  it('blocks tasks whose dependencies are not done', () => {
    const blocked = blockedTaskIds(['a', 'b'], [{ from: 'a', to: 'b' }], { a: 'running' })
    expect(blocked.has('b')).toBe(true)
  })

  it('unblocks when the dependency completes', () => {
    const blocked = blockedTaskIds(['a', 'b'], [{ from: 'a', to: 'b' }], { a: 'done' })
    expect(blocked.has('b')).toBe(false)
  })
})
