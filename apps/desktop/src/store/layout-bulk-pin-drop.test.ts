import { beforeEach, describe, expect, it } from 'vitest'

import { $pinnedSessionIds, dropSessionPins, restoreSessionPins } from './layout'

beforeEach(() => {
  $pinnedSessionIds.set([])
})

/** Pull just the ids off a batch of rollback tokens, in the order returned —
 *  the frozen `rankById` map is an implementation detail callers never
 *  compare directly. */
function ids(tokens: ReturnType<typeof dropSessionPins>): string[] {
  return tokens.map(token => token.id)
}

describe('dropSessionPins', () => {
  it('drops every named pin in one write and reports one token per removed id', () => {
    $pinnedSessionIds.set(['a', 'b', 'c', 'd'])

    const dropped = dropSessionPins(['b', 'd'])

    expect(ids(dropped)).toEqual(['b', 'd'])
    expect(dropped.every(token => token.rankById.get(token.id) === (token.id === 'b' ? 1 : 3))).toBe(true)
    expect($pinnedSessionIds.get()).toEqual(['a', 'c'])
  })

  it('ignores blank and unpinned ids without touching the list', () => {
    $pinnedSessionIds.set(['a'])

    expect(dropSessionPins([null, undefined, '  ', 'stranger'])).toEqual([])
    expect($pinnedSessionIds.get()).toEqual(['a'])
  })

  it('collapses a stored id and its lineage-root pin id to one removal each', () => {
    // archiveSession/removeSession pass both, because a compressed session's
    // pin is keyed on the lineage root while the row carries the live tip.
    $pinnedSessionIds.set(['root', 'other'])

    expect(ids(dropSessionPins(['tip', 'root']))).toEqual(['root'])
    expect($pinnedSessionIds.get()).toEqual(['other'])
  })
})

describe('restoreSessionPins', () => {
  it('puts a rolled-back pin back at the index it held', () => {
    $pinnedSessionIds.set(['a', 'b', 'c'])
    const dropped = dropSessionPins(['b'])

    restoreSessionPins(dropped)

    expect($pinnedSessionIds.get()).toEqual(['a', 'b', 'c'])
  })

  it('does not resurrect a sibling row un-pinned while its RPC was in flight', () => {
    // The whole point of the read-modify-write pair: two bulk rows overlap, the
    // first fails and rolls back, and the second's un-pin must survive it. A
    // caller that wrote back a whole pre-await snapshot would undo both.
    $pinnedSessionIds.set(['keep', 'fails', 'succeeds'])

    const failing = dropSessionPins(['fails'])

    dropSessionPins(['succeeds'])
    restoreSessionPins(failing)

    expect($pinnedSessionIds.get()).toEqual(['keep', 'fails'])
  })

  it('preserves original order when two overlapping rows both roll back', () => {
    $pinnedSessionIds.set(['a', 'b', 'c'])
    const first = dropSessionPins(['a'])
    const second = dropSessionPins(['b'])

    // RPC completion order is not deterministic. The earlier row can fail and
    // restore before the later row, even though both drops happened in order.
    restoreSessionPins(first)
    restoreSessionPins(second)

    expect($pinnedSessionIds.get()).toEqual(['a', 'b', 'c'])
  })

  it('preserves order across mixed success and failure completion orders', () => {
    for (const restoreOrder of [
      ['a', 'c'],
      ['c', 'a']
    ]) {
      $pinnedSessionIds.set(['a', 'b', 'c', 'd'])

      const dropped = new Map([
        ['a', dropSessionPins(['a'])],
        ['b', dropSessionPins(['b'])],
        ['c', dropSessionPins(['c'])]
      ])

      // b succeeds and stays removed. a and c fail in either RPC completion
      // order, and both must return around the surviving d without swapping.
      for (const id of restoreOrder) {
        restoreSessionPins(dropped.get(id) ?? [])
      }

      expect($pinnedSessionIds.get()).toEqual(['a', 'c', 'd'])
    }
  })

  it('clamps to the end when a concurrent row shortened the list', () => {
    $pinnedSessionIds.set(['a', 'b', 'c'])
    const failing = dropSessionPins(['c'])

    dropSessionPins(['a'])
    restoreSessionPins(failing)

    expect($pinnedSessionIds.get()).toEqual(['b', 'c'])
  })

  it('is a no-op for an empty rollback', () => {
    $pinnedSessionIds.set(['a'])
    restoreSessionPins([])

    expect($pinnedSessionIds.get()).toEqual(['a'])
  })

  it('is idempotent: restoring the same token twice only inserts it once', () => {
    $pinnedSessionIds.set(['a', 'b', 'c'])
    const dropped = dropSessionPins(['b'])

    restoreSessionPins(dropped)
    restoreSessionPins(dropped)

    expect($pinnedSessionIds.get()).toEqual(['a', 'b', 'c'])
  })

  it('leaves a newly pinned unknown id in its current position on restore', () => {
    $pinnedSessionIds.set(['a', 'b', 'c'])
    const dropped = dropSessionPins(['b'])

    // A pin the frozen snapshot never saw arrives while the rollback is
    // still in flight.
    $pinnedSessionIds.set(['a', 'new', 'c'])
    restoreSessionPins(dropped)

    expect($pinnedSessionIds.get()).toEqual(['a', 'new', 'b', 'c'])
  })

  describe('deterministic matrix over drop/failure subsets and restore order', () => {
    const ORIGINAL = ['a', 'b', 'c', 'd']

    // Every subset of `set`, smallest first — bounded to sets of size <= 3 by
    // the callers below, so this never blows up.
    function powerset(set: string[]): string[][] {
      return set.reduce<string[][]>((acc, id) => acc.concat(acc.map(s => [...s, id])), [[]])
    }

    // At most 2 orderings of `ids`: as-is and reversed. A full permutation
    // set would be exhaustive rather than small once `ids` grows past 2.
    function ordersOf(ids: string[]): string[][] {
      return ids.length <= 1 ? [ids] : [ids, [...ids].reverse()]
    }

    function check(dropped: string[], failed: string[], restoreOrder: string[]) {
      $pinnedSessionIds.set(ORIGINAL)

      const tokens = new Map(dropped.map(id => [id, dropSessionPins([id])]))

      for (const id of restoreOrder) {
        restoreSessionPins(tokens.get(id) ?? [])
      }

      const succeeded = new Set(dropped.filter(id => !failed.includes(id)))

      expect($pinnedSessionIds.get()).toEqual(ORIGINAL.filter(id => !succeeded.has(id)))
    }

    // Representative drop sets (single, adjacent pair, gapped pair, run of
    // three) get every failure subset and both restore orders; the full
    // 4-id drop only needs the all-fail / all-succeed extremes, since the
    // mixed-order case above already covers a gapped 4-id scenario.
    for (const dropped of [['a'], ['a', 'b'], ['b', 'd'], ['a', 'b', 'c']]) {
      for (const failed of powerset(dropped)) {
        for (const restoreOrder of ordersOf(failed)) {
          it(`drop ${JSON.stringify(dropped)}, fail ${JSON.stringify(failed)}, restore ${JSON.stringify(restoreOrder)}`, () => {
            check(dropped, failed, restoreOrder)
          })
        }
      }
    }

    for (const failed of [[], ORIGINAL]) {
      for (const restoreOrder of ordersOf(failed)) {
        it(`drop all 4, fail ${JSON.stringify(failed)}, restore ${JSON.stringify(restoreOrder)}`, () => {
          check(ORIGINAL, failed, restoreOrder)
        })
      }
    }
  })
})
