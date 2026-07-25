import { afterEach, describe, expect, it } from 'vitest'

import { $asyncDelegations, applyAsyncList, resetAsyncDelegations } from '../app/delegationStore.js'
import { buildAgentRows, DONE_LINGER_MS } from '../lib/agentRows.js'

afterEach(() => resetAsyncDelegations())

describe('applyAsyncList', () => {
  it('populates $asyncDelegations from an RPC response', () => {
    applyAsyncList({ delegations: [{ delegation_id: 'd1', goal: 'g', role: 'fixer', status: 'running' }], running: 1 })

    expect($asyncDelegations.get()).toHaveLength(1)
    expect($asyncDelegations.get()[0]!.delegation_id).toBe('d1')
  })

  it('tolerates null / undefined / missing-array payloads by clearing', () => {
    applyAsyncList({ delegations: [{ delegation_id: 'd1' }] })
    applyAsyncList(null)
    expect($asyncDelegations.get()).toEqual([])

    applyAsyncList({ delegations: [{ delegation_id: 'd1' }] })
    applyAsyncList(undefined)
    expect($asyncDelegations.get()).toEqual([])

    applyAsyncList({ delegations: [{ delegation_id: 'd1' }] })
    applyAsyncList({})
    expect($asyncDelegations.get()).toEqual([])
  })

  it('replaces (does not append) on each successive snapshot', () => {
    applyAsyncList({ delegations: [{ delegation_id: 'a' }, { delegation_id: 'b' }] })
    applyAsyncList({ delegations: [{ delegation_id: 'c' }] })

    const ids = $asyncDelegations.get().map(d => d.delegation_id)
    expect(ids).toEqual(['c'])
  })

  it('feeds buildAgentRows end-to-end from the store', () => {
    applyAsyncList({
      delegations: [
        { delegation_id: 'd1', dispatched_at: 1000, goal: 'sweep', role: 'tests', status: 'completed', completed_at: 1044 }
      ],
      running: 0
    })

    // Freshly finished: still inside the linger window, so the row is on screen
    // with its final wall-clock duration (completed_at - dispatched_at), not a
    // duration that keeps ticking against `now`.
    // `dispatched_at` / `completed_at` are epoch *seconds*; `nowMs` is millis.
    const fresh = buildAgentRows([], $asyncDelegations.get(), 1_044_000 + 5_000)
    expect(fresh.done).toBe(1)
    expect(fresh.rows[0]!.resultReady).toBe(true)
    expect(fresh.rows[0]!.elapsedSeconds).toBeCloseTo(44, 0)
  })

  it('ages finished delegations out of the panel but keeps counting them', () => {
    // The store retains completed records up to _MAX_RETAINED_COMPLETED = 50.
    // The panel must not grow with them: past DONE_LINGER_MS the row leaves the
    // screen while the header tally still reports it.
    applyAsyncList({
      delegations: [
        { delegation_id: 'd1', dispatched_at: 1000, goal: 'sweep', role: 'tests', status: 'completed', completed_at: 1044 }
      ],
      running: 0
    })

    const stale = buildAgentRows([], $asyncDelegations.get(), 1_044_000 + DONE_LINGER_MS + 1)
    expect(stale.rows).toHaveLength(0)
    expect(stale.done).toBe(1)
  })
})
