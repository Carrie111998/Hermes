import { describe, expect, it } from 'vitest'

import { sessionIdentityKey, sessionIdentityKeysFromLegacyIds } from '@/lib/session-identity'

import {
  mergeScopedSessionOrderIds,
  orderByIds,
  reconcileOrderIds,
  resolveManualSessionOrderIds,
  sameIds
} from './order'

describe('resolveManualSessionOrderIds', () => {
  it('preserves persisted order while an empty session list is still hydrating', () => {
    const persisted = [sessionIdentityKey('other', 'default'), sessionIdentityKey('shared', 'default')]

    expect(resolveManualSessionOrderIds([], persisted, true, true)).toEqual(persisted)
    expect(resolveManualSessionOrderIds([], persisted, true, false)).toEqual([])
  })

  it('does not prune persisted ids from a partially hydrated session list', () => {
    const persisted = [sessionIdentityKey('other', 'default'), sessionIdentityKey('shared', 'default')]

    expect(resolveManualSessionOrderIds([persisted[0]], persisted, true, true)).toEqual(persisted)
  })

  it('clears legacy auto-seeded order until the user manually reorders sessions', () => {
    expect(resolveManualSessionOrderIds(['newest', 'older'], ['older', 'newest'], false)).toEqual([])
  })

  it('keeps a manual order and surfaces newly seen sessions first', () => {
    expect(resolveManualSessionOrderIds(['newest', 'older', 'oldest'], ['oldest', 'older'], true)).toEqual([
      'newest',
      'oldest',
      'older'
    ])
  })

  it('clears manual order when none of the saved ids still exist', () => {
    expect(resolveManualSessionOrderIds(['newest'], ['gone'], true)).toEqual([])
  })

  it('preserves a legacy default-profile manual order after identity migration', () => {
    const current = [sessionIdentityKey('shared', 'default'), sessionIdentityKey('other', 'default')]
    const persisted = sessionIdentityKeysFromLegacyIds(['other', 'shared'])

    expect(resolveManualSessionOrderIds(current, persisted, true)).toEqual([
      sessionIdentityKey('other', 'default'),
      sessionIdentityKey('shared', 'default')
    ])
  })

  it('does not erase another profile order while its rows are unloaded', () => {
    const savedDefaultOrder = sessionIdentityKeysFromLegacyIds(['other', 'shared'])

    expect(resolveManualSessionOrderIds([sessionIdentityKey('shared', 'beta')], savedDefaultOrder, true)).toBe(
      savedDefaultOrder
    )
  })

  it('preserves hidden-profile order after reconciling a visible profile drag', () => {
    const alphaA = sessionIdentityKey('a1', 'alpha')
    const alphaB = sessionIdentityKey('a2', 'alpha')
    const betaA = sessionIdentityKey('b1', 'beta')
    const betaB = sessionIdentityKey('b2', 'beta')
    const afterDrag = [alphaB, alphaA, betaA, betaB]

    expect(resolveManualSessionOrderIds([alphaB, alphaA], afterDrag, true, false, ['alpha'])).toEqual(afterDrag)
  })

  it('drops an empty visible profile without erasing hidden-profile order', () => {
    const alphaA = sessionIdentityKey('a1', 'alpha')
    const betaA = sessionIdentityKey('b1', 'beta')

    expect(resolveManualSessionOrderIds([], [alphaA, betaA], true, false, ['alpha'])).toEqual([betaA])
  })

  it('drops stale rows for an empty profile explicitly included in a wider scope', () => {
    const alphaA = sessionIdentityKey('a1', 'alpha')
    const betaGone = sessionIdentityKey('gone', 'beta')

    expect(resolveManualSessionOrderIds([alphaA], [alphaA, betaGone], true, false, ['alpha', 'beta'])).toEqual([
      alphaA
    ])
  })
})

describe('mergeScopedSessionOrderIds', () => {
  it("reorders one profile without erasing another profile's persisted order", () => {
    const alphaA = sessionIdentityKey('a', 'alpha')
    const alphaB = sessionIdentityKey('b', 'alpha')
    const betaA = sessionIdentityKey('a', 'beta')
    const betaB = sessionIdentityKey('b', 'beta')

    expect(mergeScopedSessionOrderIds([alphaA, alphaB, betaA, betaB], [alphaB, alphaA])).toEqual([
      alphaB,
      alphaA,
      betaA,
      betaB
    ])
  })
})

describe('orderByIds', () => {
  const id = (item: { id: string }) => item.id

  it('returns items untouched when no order is given', () => {
    const items = [{ id: 'a' }, { id: 'b' }]
    expect(orderByIds(items, id, [])).toBe(items)
  })

  it('reorders by the given ids and drops missing ones', () => {
    const items = [{ id: 'a' }, { id: 'b' }, { id: 'c' }]
    expect(orderByIds(items, id, ['c', 'gone', 'a'])).toEqual([{ id: 'b' }, { id: 'c' }, { id: 'a' }])
  })

  it('surfaces items absent from the order first', () => {
    const items = [{ id: 'fresh' }, { id: 'a' }, { id: 'b' }]
    expect(orderByIds(items, id, ['b', 'a'])).toEqual([{ id: 'fresh' }, { id: 'b' }, { id: 'a' }])
  })
})

describe('reconcileOrderIds', () => {
  it('returns empty for no current ids', () => {
    expect(reconcileOrderIds([], ['a'])).toEqual([])
  })

  it('returns current ids when there is no saved order', () => {
    expect(reconcileOrderIds(['a', 'b'], [])).toEqual(['a', 'b'])
  })

  it('puts newly-seen ids ahead of the retained saved order', () => {
    expect(reconcileOrderIds(['fresh', 'a', 'b'], ['b', 'a', 'gone'])).toEqual(['fresh', 'b', 'a'])
  })
})

describe('sameIds', () => {
  it('is true only for identical ordered lists', () => {
    expect(sameIds(['a', 'b'], ['a', 'b'])).toBe(true)
    expect(sameIds(['a', 'b'], ['b', 'a'])).toBe(false)
    expect(sameIds(['a'], ['a', 'b'])).toBe(false)
  })
})
