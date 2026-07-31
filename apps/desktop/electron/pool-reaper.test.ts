import { describe, expect, it } from 'vitest'

import { partitionIdleReapable, selectLruEvictionCandidates } from './pool-reaper'

describe('partitionIdleReapable', () => {
  it('reaps local backends idle beyond the limit', () => {
    const now = 1_000_000
    const entries: Array<[string, { process: object; lastActiveAt: number }]> = [
      ['alpha', { process: {}, lastActiveAt: now - 700_000 }]
    ]

    const { reap, sparedRemote } = partitionIdleReapable(entries, now, 600_000)

    expect(reap).toEqual([{ profile: 'alpha', idleMs: 700 }])
    expect(sparedRemote).toEqual([])
  })

  it('never idle-reaps remote descriptors (no local process)', () => {
    const now = 1_000_000
    const entries: Array<[string, { process: null; lastActiveAt: number }]> = [
      ['remote-profile', { process: null, lastActiveAt: now - 86_400_000 }]
    ]

    const { reap, sparedRemote } = partitionIdleReapable(entries, now, 600_000)

    expect(reap).toEqual([])
    expect(sparedRemote).toEqual(['remote-profile'])
  })

  it('spares local backends still within the idle window', () => {
    const now = 1_000_000
    const entries: Array<[string, { process: object; lastActiveAt: number }]> = [
      ['busy', { process: {}, lastActiveAt: now - 60_000 }]
    ]

    const { reap } = partitionIdleReapable(entries, now, 600_000)

    expect(reap).toEqual([])
  })

  it('partitions a mixed pool correctly', () => {
    const now = 1_000_000
    const entries: Array<[string, { process: object | null; lastActiveAt: number }]> = [
      ['local-idle', { process: {}, lastActiveAt: now - 900_000 }],
      ['local-active', { process: {}, lastActiveAt: now - 5_000 }],
      ['remote-idle', { process: null, lastActiveAt: now - 900_000 }]
    ]

    const { reap, sparedRemote } = partitionIdleReapable(entries, now, 600_000)

    expect(reap.map(r => r.profile)).toEqual(['local-idle'])
    expect(sparedRemote).toEqual(['remote-idle'])
  })
})

describe('selectLruEvictionCandidates', () => {
  it('excludes remote descriptors from LRU eviction', () => {
    const now = 1_000_000
    const entries: Array<[string, { process: object | null; lastActiveAt: number }]> = [
      ['remote', { process: null, lastActiveAt: 0 }],
      ['local-stale', { process: {}, lastActiveAt: now - 200_000 }],
      ['local-fresh', { process: {}, lastActiveAt: now - 10_000 }]
    ]

    expect(selectLruEvictionCandidates(entries, now, 90_000)).toEqual(['local-stale'])
  })

  it('orders candidates least-recently-used first', () => {
    const now = 1_000_000
    const entries: Array<[string, { process: object; lastActiveAt: number }]> = [
      ['newer-stale', { process: {}, lastActiveAt: now - 150_000 }],
      ['older-stale', { process: {}, lastActiveAt: now - 500_000 }]
    ]

    expect(selectLruEvictionCandidates(entries, now, 90_000)).toEqual(['older-stale', 'newer-stale'])
  })

  it('spares entries whose renderer socket is still fresh', () => {
    const now = 1_000_000
    const entries: Array<[string, { process: object; lastActiveAt: number }]> = [
      ['live', { process: {}, lastActiveAt: now - 30_000 }]
    ]

    expect(selectLruEvictionCandidates(entries, now, 90_000)).toEqual([])
  })
})
