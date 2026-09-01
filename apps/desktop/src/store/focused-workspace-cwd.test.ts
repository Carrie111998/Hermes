import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { group } from '@/components/pane-shell/tree/model'
import { $layoutTree, declareDefaultTree, noteActiveTreeGroup } from '@/components/pane-shell/tree/store'
import {
  $selectedStoredSessionId,
  commitWorkspaceCwdForSelectedSession,
  releaseWorkspaceCwdOwner,
  setCurrentCwdTransient,
  setSessions,
  setWorkspaceCwdOwner
} from '@/store/session'
import { $focusedWorkspaceCwd, $sessionStates, $sessionTiles } from '@/store/session-states'

const row = (id: string, cwd: null | string) =>
  ({
    archived: false,
    cwd,
    ended_at: null,
    id,
    input_tokens: 0,
    is_active: true,
    last_active: 0,
    message_count: 1,
    model: null,
    output_tokens: 0,
    started_at: 0,
    title: id
  }) as never

const slice = (storedSessionId: string, cwd: string) =>
  ({ cwd, storedSessionId }) as unknown as ClientSessionState

/** Focus a TILE the way the layout tree does: its pane is the active tab. */
function focusTile(storedSessionId: string) {
  $sessionTiles.set([{ storedSessionId }])
  declareDefaultTree(
    group(['workspace', `session-tile:${storedSessionId}`], {
      active: `session-tile:${storedSessionId}`,
      id: 'grp-main'
    })
  )
  noteActiveTreeGroup('grp-main')
}

describe('$focusedWorkspaceCwd', () => {
  beforeEach(() => {
    window.localStorage.clear()
    $sessionStates.set({})
    $sessionTiles.set([])
    $layoutTree.set(null)
    noteActiveTreeGroup(null)
    setSessions(() => [])
    $selectedStoredSessionId.set(null)
    setCurrentCwdTransient('')
    setWorkspaceCwdOwner(null)
  })

  afterEach(() => {
    $sessionStates.set({})
    $sessionTiles.set([])
    $layoutTree.set(null)
    noteActiveTreeGroup(null)
    setSessions(() => [])
    $selectedStoredSessionId.set(null)
    setCurrentCwdTransient('')
    setWorkspaceCwdOwner(null)
  })

  // THE regression, measured in the running app: a base-image tile was focused
  // while the primary chat sat in main-quarkus. The Files pane read the
  // `$currentCwd` singleton — which describes the PRIMARY chat, because a tile's
  // runtime deliberately never publishes into it (`foreground: false`) — and so
  // listed main-quarkus with header MAIN-QUARKUS, while the statusbar correctly
  // said base-image.
  it('follows a focused TILE, not the primary chat singleton', () => {
    $selectedStoredSessionId.set('sess-primary')
    setSessions(() => [row('sess-primary', '/main-quarkus'), row('sess-tile', '/base-image')])
    commitWorkspaceCwdForSelectedSession('/main-quarkus')
    focusTile('sess-tile')

    expect($focusedWorkspaceCwd.get()).toBe('/base-image')
  })

  it('prefers the tile runtime slice over its stored row', () => {
    $selectedStoredSessionId.set('sess-primary')
    setSessions(() => [row('sess-primary', '/main-quarkus'), row('sess-tile', '/base-image')])
    commitWorkspaceCwdForSelectedSession('/main-quarkus')
    focusTile('sess-tile')
    $sessionTiles.set([{ runtimeId: 'rt-tile', storedSessionId: 'sess-tile' }])
    $sessionStates.set({ 'rt-tile': slice('sess-tile', '/base-image/.worktrees/feature') })

    expect($focusedWorkspaceCwd.get()).toBe('/base-image/.worktrees/feature')
  })

  // A tile with no workspace stays empty rather than naming another project.
  it('stays empty for a detached focused tile', () => {
    $selectedStoredSessionId.set('sess-primary')
    setSessions(() => [row('sess-primary', '/main-quarkus'), row('sess-tile', null)])
    commitWorkspaceCwdForSelectedSession('/main-quarkus')
    focusTile('sess-tile')

    expect($focusedWorkspaceCwd.get()).toBe('')
  })

  it('uses the primary singleton when the primary chat is focused and owns it', () => {
    $selectedStoredSessionId.set('sess-primary')
    setSessions(() => [row('sess-primary', null)])
    commitWorkspaceCwdForSelectedSession('/main-quarkus')

    expect($focusedWorkspaceCwd.get()).toBe('/main-quarkus')
  })

  // Ownership gates the singleton rung only: during a switch it still names the
  // conversation the user just left (ae6eb578bb).
  it('ignores an un-owned singleton', () => {
    $selectedStoredSessionId.set('sess-switching')
    setSessions(() => [])
    setCurrentCwdTransient('/previous-project')
    releaseWorkspaceCwdOwner()

    expect($focusedWorkspaceCwd.get()).toBe('')
  })

  // …but it must NOT gate the ROW rung: a released marker says nothing about the
  // row, so dropping it too would blank a workspace we do know (416e025c46).
  it('still uses the stored row while ownership is released', () => {
    $selectedStoredSessionId.set('sess-known')
    setSessions(() => [row('sess-known', '/its-own-project')])
    setCurrentCwdTransient('/previous-project')
    releaseWorkspaceCwdOwner()

    expect($focusedWorkspaceCwd.get()).toBe('/its-own-project')
  })

  it('labels a fresh draft, whose null owner matches its null selection', () => {
    setCurrentCwdTransient('/draft-target')
    setWorkspaceCwdOwner(null)

    expect($focusedWorkspaceCwd.get()).toBe('/draft-target')
  })

  it('is reactive: focusing away from the tile returns the primary workspace', () => {
    $selectedStoredSessionId.set('sess-primary')
    setSessions(() => [row('sess-primary', '/main-quarkus'), row('sess-tile', '/base-image')])
    commitWorkspaceCwdForSelectedSession('/main-quarkus')
    focusTile('sess-tile')
    expect($focusedWorkspaceCwd.get()).toBe('/base-image')

    noteActiveTreeGroup(null)
    declareDefaultTree(group(['workspace'], { active: 'workspace', id: 'grp-main' }))

    expect($focusedWorkspaceCwd.get()).toBe('/main-quarkus')
  })
})

/**
 * The precedence contract, enumerated.
 *
 * The three rungs interact through two decisions that are easy to reorder by
 * accident: the `row && !primaryOwnsSingleton -> ''` early return (a row that
 * exists and names nothing reads as detached) and the `primaryFocused &&
 * primaryIsOwned` singleton rung. The cases above each pin one failure mode;
 * this table pins the ORDER, so a refactor that swaps two rungs fails here
 * instead of silently changing which project a surface names.
 *
 * Axes: the focused row's cwd (a path / present-but-null / no row at all) x
 * which surface holds focus (primary = selection, tile = never the selection,
 * none = fresh draft) x whether the selected conversation owns `$currentCwd`.
 */
describe('$focusedWorkspaceCwd precedence', () => {
  const SINGLETON = '/singleton-project'
  const ROW = '/row-project'

  type Focus = 'none' | 'primary' | 'tile'
  type RowShape = 'absent' | 'null-cwd' | 'path'

  interface Case {
    /** Expected cwd — '' means "name no project rather than the wrong one". */
    expected: string
    focus: Focus
    owned: boolean
    rowShape: RowShape
    why: string
  }

  const cases: Case[] = [
    // Rung 2 wins outright: a row that names a path is the answer regardless of
    // who owns the singleton and of which surface is focused.
    { expected: ROW, focus: 'primary', owned: true, rowShape: 'path', why: 'row outranks the owned singleton' },
    { expected: ROW, focus: 'primary', owned: false, rowShape: 'path', why: 'row survives released ownership' },
    { expected: ROW, focus: 'tile', owned: true, rowShape: 'path', why: "tile's own row, not the primary's" },
    { expected: ROW, focus: 'tile', owned: false, rowShape: 'path', why: "tile's own row, ownership irrelevant" },

    // Row present but null-cwd: detached UNLESS this very conversation owns the
    // singleton, which is a positive claim and outranks a not-yet-backfilled row.
    { expected: SINGLETON, focus: 'primary', owned: true, rowShape: 'null-cwd', why: 'own claim beats an unbackfilled row' },
    { expected: '', focus: 'primary', owned: false, rowShape: 'null-cwd', why: 'no claim, no row: detached' },
    { expected: '', focus: 'tile', owned: true, rowShape: 'null-cwd', why: 'the claim is the PRIMARY\'s, not the tile\'s' },
    { expected: '', focus: 'tile', owned: false, rowShape: 'null-cwd', why: 'detached tile stays empty' },

    // No row at all (fresh/unlisted): the early return cannot fire, so the
    // singleton rung decides — and only for the primary.
    { expected: SINGLETON, focus: 'primary', owned: true, rowShape: 'absent', why: 'unlisted primary falls to its own claim' },
    { expected: '', focus: 'primary', owned: false, rowShape: 'absent', why: 'unowned singleton is never adopted' },
    { expected: '', focus: 'tile', owned: true, rowShape: 'absent', why: 'a tile never inherits the primary workspace' },
    { expected: '', focus: 'tile', owned: false, rowShape: 'absent', why: 'a tile never inherits the primary workspace' },

    // Fresh draft: no selection, no tile. Owner null matches selection null.
    { expected: SINGLETON, focus: 'none', owned: true, rowShape: 'absent', why: 'draft owns its own target folder' },
    { expected: '', focus: 'none', owned: false, rowShape: 'absent', why: 'released draft names nothing' }
  ]

  beforeEach(() => {
    window.localStorage.clear()
    $sessionStates.set({})
    $sessionTiles.set([])
    $layoutTree.set(null)
    noteActiveTreeGroup(null)
    setSessions(() => [])
    $selectedStoredSessionId.set(null)
    setCurrentCwdTransient('')
    setWorkspaceCwdOwner(null)
  })

  for (const c of cases) {
    it(`${c.focus} focus + ${c.rowShape} row + ${c.owned ? 'owned' : 'unowned'} singleton -> ${c.expected || "''"} (${c.why})`, () => {
      // The focused id is DERIVED, never set: a tile is focused by making its
      // pane the active tab, the primary by being the selection with no tile.
      const focusedId = c.focus === 'tile' ? 'sess-tile' : 'sess-primary'
      const selectedId = c.focus === 'none' ? null : 'sess-primary'

      $selectedStoredSessionId.set(selectedId)

      const rows: ReturnType<typeof row>[] = []

      if (c.focus === 'tile' && selectedId) {
        rows.push(row(selectedId, '/primary-project'))
      }

      if (c.rowShape !== 'absent') {
        rows.push(row(focusedId, c.rowShape === 'path' ? ROW : null))
      }

      setSessions(() => rows)

      // Ownership is a marker, not a path: commit claims the singleton for the
      // current selection, release leaves the path and drops the claim.
      setCurrentCwdTransient(SINGLETON)

      if (c.owned) {
        setWorkspaceCwdOwner(selectedId)
      } else {
        releaseWorkspaceCwdOwner()
      }

      if (c.focus === 'tile') {
        focusTile(focusedId)
      }

      expect($focusedWorkspaceCwd.get()).toBe(c.expected)
    })
  }
})
