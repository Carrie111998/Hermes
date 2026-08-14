import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { registry } from '@/contrib/registry'

import { group, type LayoutNode, split, type SplitNode } from './model'
import {
  $hiddenTreePanes,
  $layoutEqualizeMotion,
  $layoutTree,
  cancelLayoutEqualizeMotion,
  equalizeVisibleSessionPanes,
  LAYOUT_EQUALIZE_MOTION_MS,
  setTreeGroupMinimized
} from './store'

const disposers: (() => void)[] = []

function registerPane(id: string, placement: 'left' | 'main' | 'right' = 'main') {
  disposers.push(registry.register({ area: 'panes', data: { placement }, id, render: () => null, title: id }))
}

function childSplit(node: LayoutNode, index: number): SplitNode {
  const child = (node as SplitNode).children[index]

  if (child.type !== 'split') {
    throw new Error(`Expected child ${index} to be a split`)
  }

  return child
}

beforeEach(() => {
  window.localStorage.clear()
  $hiddenTreePanes.set(new Set())

  for (const id of [
    'workspace',
    'session-tile:a',
    'session-tile:b',
    'session-tile:c',
    'session-tile:d',
    'session-tile:hidden',
    'files',
    'preview',
    'page'
  ]) {
    registerPane(id, id === 'files' ? 'left' : id === 'preview' ? 'right' : 'main')
  }
})

afterEach(() => {
  cancelLayoutEqualizeMotion()
  vi.useRealTimers()
  disposers.splice(0).forEach(dispose => dispose())
})

describe('equalizeVisibleSessionPanes', () => {
  for (const orientation of ['row', 'column'] as const) {
    it(`gives visible conversations equal ${orientation} shares`, () => {
      $layoutTree.set(
        split(
          orientation,
          [
            group(['workspace'], { id: 'g-workspace' }),
            group(['session-tile:a'], { id: 'g-a' }),
            group(['session-tile:b'], { id: 'g-b' })
          ],
          [2, 7, 3],
          's-root'
        )
      )

      equalizeVisibleSessionPanes()

      expect(($layoutTree.get() as SplitNode).weights).toEqual([4, 4, 4])
      expect(JSON.parse(window.localStorage.getItem('hermes.desktop.layoutTree.v2') ?? 'null').weights).toEqual([
        4, 4, 4
      ])
    })
  }

  it('uses descendant conversation counts in nested mixed-axis layouts', () => {
    $layoutTree.set(
      split(
        'row',
        [
          group(['files'], { id: 'g-left' }),
          group(['workspace'], { id: 'g-workspace' }),
          split(
            'column',
            [group(['session-tile:a'], { id: 'g-a' }), group(['session-tile:b'], { id: 'g-b' })],
            [9, 1],
            's-nested'
          ),
          group(['preview'], { id: 'g-right' })
        ],
        [7, 5, 1, 11],
        's-root'
      )
    )

    equalizeVisibleSessionPanes()

    const result = $layoutTree.get() as SplitNode

    // The left/right sections keep their allocation. The two conversation-
    // bearing branches keep their combined weight (6), divided 1:2 because
    // the nested branch contains two visible conversations.
    expect(result.weights).toEqual([7, 2, 4, 11])
    expect(childSplit(result, 2).weights).toEqual([5, 5])
  })

  it('counts only the pane a group actually shows', () => {
    $hiddenTreePanes.set(new Set(['session-tile:hidden']))
    $layoutTree.set(
      split(
        'row',
        [
          group(['session-tile:a', 'page'], { active: 'page', id: 'g-page' }),
          group(['session-tile:b', 'session-tile:hidden'], { active: 'session-tile:hidden', id: 'g-hidden' }),
          group(['session-tile:c'], { id: 'g-minimized', minimized: true }),
          group(['session-tile:missing', 'session-tile:d'], { active: 'session-tile:missing', id: 'g-missing' })
        ],
        [10, 2, 20, 6],
        's-root'
      )
    )

    equalizeVisibleSessionPanes()

    // g-page shows a non-session tab; g-minimized shows no body. The hidden
    // and unregistered active ids fall back to B and D, the same way TreeGroup
    // renders them, so only those two tracks are equalized.
    expect(($layoutTree.get() as SplitNode).weights).toEqual([10, 4, 20, 4])
  })

  it('preserves the tree reference when fewer than two conversations are visible', () => {
    const original = split(
      'row',
      [group(['workspace'], { id: 'g-workspace' }), group(['page'], { id: 'g-page' })],
      [3, 7],
      's-root'
    )

    $layoutTree.set(original)
    equalizeVisibleSessionPanes()

    expect($layoutTree.get()).toBe(original)
    expect(window.localStorage.getItem('hermes.desktop.layoutTree.v2')).toBeNull()
  })

  it('preserves the tree reference when visible conversations are already balanced', () => {
    const original = split(
      'column',
      [group(['workspace'], { id: 'g-workspace' }), group(['session-tile:a'], { id: 'g-a' })],
      [1, 1],
      's-root'
    )

    $layoutTree.set(original)
    equalizeVisibleSessionPanes()

    expect($layoutTree.get()).toBe(original)
  })

  it('opens one bounded motion window only when equalization changes the tree', () => {
    vi.useFakeTimers()
    $layoutTree.set(
      split(
        'row',
        [group(['session-tile:a'], { id: 'g-a' }), group(['session-tile:b'], { id: 'g-b' })],
        [3, 1],
        's-root'
      )
    )

    equalizeVisibleSessionPanes()

    expect($layoutEqualizeMotion.get()).toBe(true)
    vi.advanceTimersByTime(LAYOUT_EQUALIZE_MOTION_MS)
    expect($layoutEqualizeMotion.get()).toBe(false)

    equalizeVisibleSessionPanes()
    expect($layoutEqualizeMotion.get()).toBe(false)
  })

  it('ends equalization motion before an unrelated layout mutation', () => {
    vi.useFakeTimers()
    $layoutTree.set(
      split(
        'row',
        [group(['session-tile:a'], { id: 'g-a' }), group(['session-tile:b'], { id: 'g-b' })],
        [3, 1],
        's-root'
      )
    )

    equalizeVisibleSessionPanes()
    expect($layoutEqualizeMotion.get()).toBe(true)

    setTreeGroupMinimized('g-a', true)

    expect($layoutEqualizeMotion.get()).toBe(false)
  })
})
