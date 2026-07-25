import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { group, split } from '@/components/pane-shell/tree/model'
import type { SessionTile } from '@/store/session-states'
import { orderTilesByTree, selectionHomesToWorkspace } from '@/store/session-states'

import { setSelectedStoredSessionId } from './session'
import { $sessionTiles, focusOpenSession } from './session-states'

const SESSION_A = 'session-a'

const tile = (storedSessionId: string): SessionTile => ({ storedSessionId })
const tilePane = (id: string) => `session-tile:${id}`

beforeEach(() => {
  // Start clean: no selected session, no tiles, path at root.
  setSelectedStoredSessionId(null)
  $sessionTiles.set([])
  window.history.pushState({}, '', '/')
})

afterEach(() => {
  setSelectedStoredSessionId(null)
  $sessionTiles.set([])
  window.history.pushState({}, '', '/')
})

describe('orderTilesByTree', () => {
  it('no-ops (null) without a tree or below two tiles', () => {
    expect(orderTilesByTree(null, [tile('a'), tile('b')])).toBeNull()
    expect(orderTilesByTree(group([tilePane('a')]), [tile('a')])).toBeNull()
  })

  it('reorders tiles to layout-tree encounter order across a split', () => {
    const tree = split('row', [group(['workspace', tilePane('b')]), group([tilePane('a')])])

    expect(orderTilesByTree(tree, [tile('a'), tile('b')])).toEqual([tile('b'), tile('a')])
  })

  it('returns null when the array already matches strip order (skip persist)', () => {
    const tree = split('row', [group([tilePane('b')]), group([tilePane('a')])])

    expect(orderTilesByTree(tree, [tile('b'), tile('a')])).toBeNull()
  })

  it('sorts not-yet-adopted tiles after placed ones, stably', () => {
    const tree = group(['workspace', tilePane('b')])

    expect(orderTilesByTree(tree, [tile('a'), tile('b'), tile('c')])).toEqual([tile('b'), tile('a'), tile('c')])
  })
})

describe('selectionHomesToWorkspace', () => {
  const tiles = [tile('a'), tile('b')]

  it('homes for a null selection or a non-tile session', () => {
    expect(selectionHomesToWorkspace(null, tiles)).toBe(true)
    expect(selectionHomesToWorkspace('c', tiles)).toBe(true)
  })

  it('skips homing when the selected id is already an open tile', () => {
    expect(selectionHomesToWorkspace('a', tiles)).toBe(false)
  })
})

describe('focusOpenSession', () => {
  it('returns true when the session is an open tile', () => {
    $sessionTiles.set([
      { storedSessionId: SESSION_A }
    ])

    expect(focusOpenSession(SESSION_A)).toBe(true)
  })

  it('returns true when the session is selected and current route is a session route', () => {
    setSelectedStoredSessionId(SESSION_A)
    window.history.pushState({}, '', `/${SESSION_A}`)

    expect(focusOpenSession(SESSION_A)).toBe(true)
  })

  it('returns false when the session is selected but current route is NOT a session route', () => {
    setSelectedStoredSessionId(SESSION_A)
    window.history.pushState({}, '', '/messaging')

    expect(focusOpenSession(SESSION_A)).toBe(false)
  })

  it('returns false when the session is selected but route is a reserved page like /skills', () => {
    setSelectedStoredSessionId(SESSION_A)
    window.history.pushState({}, '', '/skills')

    expect(focusOpenSession(SESSION_A)).toBe(false)
  })

  it('returns false when the session does not match anything', () => {
    setSelectedStoredSessionId(SESSION_A)

    expect(focusOpenSession('other-session')).toBe(false)
  })

  it('returns false when no session is selected at all', () => {
    window.history.pushState({}, '', '/messaging')

    expect(focusOpenSession(SESSION_A)).toBe(false)
  })

  it('returns true for a tile session even when on a non-session route', () => {
    $sessionTiles.set([
      { storedSessionId: SESSION_A }
    ])
    window.history.pushState({}, '', '/messaging')

    // Tiles are always navigated via revealTreePane, never via navigate.
    expect(focusOpenSession(SESSION_A)).toBe(true)
  })
})
