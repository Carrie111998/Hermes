import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  $bulkSessionActions,
  $selectedSessionIds,
  $selectionModeActive,
  clearSessionSelection,
  enterSelectionMode,
  forgetSessionRowOrder,
  pruneSessionSelection,
  registerBulkSessionActions,
  registerSessionRowOrder,
  selectSessionRange,
  toggleSessionSelection
} from './session-selection'

beforeEach(() => {
  clearSessionSelection()
  registerBulkSessionActions(null)
  forgetSessionRowOrder('test')
})

describe('enterSelectionMode', () => {
  it('turns selection mode on with exactly the given row selected', () => {
    enterSelectionMode('a')

    expect($selectionModeActive.get()).toBe(true)
    expect($selectedSessionIds.get()).toEqual(['a'])
  })
})

describe('toggleSessionSelection', () => {
  it('adds the row on first toggle and removes it on the second', () => {
    toggleSessionSelection('test', 'a')
    expect($selectedSessionIds.get()).toEqual(['a'])

    toggleSessionSelection('test', 'a')
    expect($selectedSessionIds.get()).toEqual([])
  })

  it('does not force selection mode off when a toggle empties the selection', () => {
    enterSelectionMode('a')
    toggleSessionSelection('test', 'a')

    expect($selectedSessionIds.get()).toEqual([])
    expect($selectionModeActive.get()).toBe(true)
  })
})

describe('selectSessionRange', () => {
  it('selects everything between the anchor and the target, in row order', () => {
    registerSessionRowOrder('test', ['a', 'b', 'c', 'd'])
    toggleSessionSelection('test', 'a')

    selectSessionRange('test', 'd')

    expect($selectedSessionIds.get()).toEqual(['a', 'b', 'c', 'd'])
  })

  it('ranges backward from the anchor just as well', () => {
    registerSessionRowOrder('test', ['a', 'b', 'c', 'd'])
    toggleSessionSelection('test', 'd')

    selectSessionRange('test', 'b')

    expect($selectedSessionIds.get()).toEqual(['b', 'c', 'd'])
  })

  it('falls back to a plain toggle when the scope has no registered order', () => {
    toggleSessionSelection('unregistered', 'a')

    selectSessionRange('unregistered', 'b')

    expect($selectedSessionIds.get()).toEqual(['a', 'b'])
  })

  it('falls back to a plain toggle when there is no anchor yet', () => {
    registerSessionRowOrder('test', ['a', 'b', 'c'])

    selectSessionRange('test', 'b')

    expect($selectedSessionIds.get()).toEqual(['b'])
  })
})

describe('clearSessionSelection', () => {
  it('empties the selection and turns selection mode off', () => {
    enterSelectionMode('a')
    clearSessionSelection()

    expect($selectedSessionIds.get()).toEqual([])
    expect($selectionModeActive.get()).toBe(false)
  })

  it('drops the anchor too, so a later range starts fresh', () => {
    registerSessionRowOrder('test', ['a', 'b', 'c'])
    toggleSessionSelection('test', 'a')
    clearSessionSelection()

    selectSessionRange('test', 'c')

    // No anchor survives the clear, so this ranges-via-toggle onto just 'c'.
    expect($selectedSessionIds.get()).toEqual(['c'])
  })
})

describe('pruneSessionSelection', () => {
  it('drops only the named ids, leaving mode and the rest of the selection alone', () => {
    enterSelectionMode('a')
    toggleSessionSelection('test', 'b')
    toggleSessionSelection('test', 'c')

    pruneSessionSelection(['b'])

    expect($selectedSessionIds.get()).toEqual(['a', 'c'])
    expect($selectionModeActive.get()).toBe(true)
  })
})

describe('registerBulkSessionActions', () => {
  it('publishes and clears the bulk actions handle', () => {
    const actions = { archive: vi.fn(), remove: vi.fn() }
    registerBulkSessionActions(actions)

    expect($bulkSessionActions.get()).toBe(actions)

    registerBulkSessionActions(null)
    expect($bulkSessionActions.get()).toBeNull()
  })
})
