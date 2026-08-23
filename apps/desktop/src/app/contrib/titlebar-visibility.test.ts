import { afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'

import { allPaneIds, findGroupOfPane, group, split } from '@/components/pane-shell/tree/model'
import { rootChildSide } from '@/components/pane-shell/tree/renderer/track-model'
import {
  $collapsedTreeSides,
  $dismissedPanes,
  $hiddenTreePanes,
  $layoutTree,
  bindPaneVisibility,
  declareDefaultTree,
  isPaneVisible,
  mirrorLayoutTree,
  resetLayoutTree
} from '@/components/pane-shell/tree/store'
import { registry } from '@/contrib/registry'
import { $fileBrowserOpen, FILE_BROWSER_PANE_ID, setFileBrowserOpen, setSidebarOpen } from '@/store/layout'
import { getPaneStateSnapshot, setPaneWidthOverride } from '@/store/panes'

import { wireTitlebarVisibility } from './titlebar-visibility'

const disposers: (() => void)[] = []

const defaultTree = split('row', [
  group(['sessions'], { active: 'sessions', id: 'default-sessions' }),
  group(['workspace'], { active: 'workspace', id: 'default-workspace' }),
  group(['files'], { active: 'files', id: 'default-files' })
])

function currentTree() {
  const tree = $layoutTree.get()

  if (!tree || tree.type !== 'split') {
    throw new Error('expected a root split')
  }

  return tree
}

function paneVisible(id: string) {
  const tree = currentTree()
  const child = tree.children.find(node => allPaneIds(node).includes(id))
  const paneFor = (paneId: string) => registry.getArea('panes').find(pane => pane.id === paneId)
  const side = child ? rootChildSide(child, paneFor) : null

  return Boolean(child && isPaneVisible(id) && (side === null || !$collapsedTreeSides.get().has(side)))
}

beforeAll(() => {
  declareDefaultTree(defaultTree)
  wireTitlebarVisibility()
  bindPaneVisibility('files', $fileBrowserOpen, () => setFileBrowserOpen(false), () => setFileBrowserOpen(true))
})

beforeEach(() => {
  window.localStorage.clear()
  $layoutTree.set(null)
  $hiddenTreePanes.set(new Set())
  $dismissedPanes.set(new Set())
  $collapsedTreeSides.set(new Set())
  setSidebarOpen(true)
  setFileBrowserOpen(true)
  setPaneWidthOverride(FILE_BROWSER_PANE_ID, undefined)

  for (const [id, placement] of [
    ['sessions', 'left'],
    ['plugin:left', 'left'],
    ['workspace', 'main'],
    ['plugin:pane', 'right'],
    ['files', 'right']
  ] as const) {
    disposers.push(
      registry.register({
        area: 'panes',
        data: id === 'plugin:pane' ? { dock: { pane: 'files', pos: 'left' }, placement } : { placement },
        id,
        render: () => null,
        title: id
      })
    )
  }

  $layoutTree.set(
    split(
      'row',
      [
        group(['sessions'], { active: 'sessions', id: 'g-sessions' }),
        group(['plugin:left'], { active: 'plugin:left', id: 'g-plugin-left' }),
        group(['workspace'], { active: 'workspace', id: 'g-workspace' }),
        group(['plugin:pane'], { active: 'plugin:pane', id: 'g-plugin' }),
        group(['files'], { active: 'files', id: 'g-files' })
      ],
      [1, 2, 5, 3, 2]
    )
  )
})

afterEach(() => {
  disposers.splice(0).forEach(dispose => dispose())
  setSidebarOpen(true)
  setFileBrowserOpen(true)
  $layoutTree.set(null)
  $hiddenTreePanes.set(new Set())
  $dismissedPanes.set(new Set())
  $collapsedTreeSides.set(new Set())
  window.localStorage.clear()
})

describe('titlebar visibility wiring', () => {
  it('hides Files without hiding a sibling plugin pane', () => {
    const tree = currentTree()

    expect($fileBrowserOpen.get()).toBe(true)
    expect(paneVisible('workspace')).toBe(true)
    expect(paneVisible('plugin:pane')).toBe(true)
    expect(paneVisible('files')).toBe(true)

    setFileBrowserOpen(false)

    expect($fileBrowserOpen.get()).toBe(false)
    expect(paneVisible('workspace')).toBe(true)
    expect(paneVisible('plugin:pane')).toBe(true)
    expect(paneVisible('files')).toBe(false)
    expect(allPaneIds(tree)).toContain('plugin:pane')
  })

  it('restores Files while the sibling plugin pane stays visible', () => {
    setFileBrowserOpen(false)
    setFileBrowserOpen(true)

    expect(paneVisible('files')).toBe(true)
    expect(paneVisible('plugin:pane')).toBe(true)
  })

  it('restores Files on layout reset without changing a sibling plugin pane', () => {
    const contribution = registry.getArea('panes').find(pane => pane.id === 'plugin:pane')

    setFileBrowserOpen(false)
    expect($fileBrowserOpen.get()).toBe(false)
    expect(paneVisible('files')).toBe(false)

    resetLayoutTree()

    expect($fileBrowserOpen.get()).toBe(true)
    expect(paneVisible('files')).toBe(true)
    expect(registry.getArea('panes').find(pane => pane.id === 'plugin:pane')).toBe(contribution)
    expect(paneVisible('plugin:pane')).toBe(true)
  })

  it('retains the Files group, split shares, and width through close and reopen', () => {
    const tree = currentTree()
    const filesGroup = findGroupOfPane(tree, 'files')
    const splitShares = [...tree.weights]
    setPaneWidthOverride(FILE_BROWSER_PANE_ID, 288)

    setFileBrowserOpen(false)
    setFileBrowserOpen(true)

    expect(currentTree()).toBe(tree)
    expect(findGroupOfPane(currentTree(), 'files')).toBe(filesGroup)
    expect(currentTree().weights).toEqual(splitShares)
    expect(getPaneStateSnapshot(FILE_BROWSER_PANE_ID)).toMatchObject({ open: true, widthOverride: 288 })
  })

  it('repeats close and open deterministically', () => {
    const tree = currentTree()

    for (let cycle = 0; cycle < 3; cycle += 1) {
      setFileBrowserOpen(false)
      expect($hiddenTreePanes.get().has('files')).toBe(true)
      expect(paneVisible('plugin:pane')).toBe(true)

      setFileBrowserOpen(true)
      expect($hiddenTreePanes.get().has('files')).toBe(false)
      expect(paneVisible('files')).toBe(true)
    }

    expect(currentTree()).toBe(tree)
  })

  it('does not collapse or mutate siblings when Files is missing', () => {
    const initial = currentTree()
    const withoutFiles = split('row', initial.children.slice(0, -1), initial.weights.slice(0, -1), initial.id)
    const pluginGroup = findGroupOfPane(withoutFiles, 'plugin:pane')
    $layoutTree.set(withoutFiles)

    setFileBrowserOpen(false)

    expect(currentTree()).toBe(withoutFiles)
    expect(findGroupOfPane(currentTree(), 'plugin:pane')).toBe(pluginGroup)
    expect(paneVisible('plugin:pane')).toBe(true)
    expect($collapsedTreeSides.get().has('right')).toBe(false)

    setFileBrowserOpen(true)

    expect(currentTree()).toBe(withoutFiles)
    expect(allPaneIds(currentTree())).not.toContain('files')
  })

  it('targets Files by id after the tree is mirrored', () => {
    mirrorLayoutTree()
    expect(allPaneIds(currentTree())).toEqual(['files', 'plugin:pane', 'workspace', 'plugin:left', 'sessions'])

    setFileBrowserOpen(false)

    expect(paneVisible('files')).toBe(false)
    expect(paneVisible('plugin:pane')).toBe(true)
    expect(paneVisible('workspace')).toBe(true)
    expect($collapsedTreeSides.get().has('right')).toBe(false)
  })

  it('keeps the Sessions control as a whole-left-side collapse', () => {
    setSidebarOpen(false)

    expect($collapsedTreeSides.get().has('left')).toBe(true)
    expect(paneVisible('sessions')).toBe(false)
    expect(paneVisible('plugin:left')).toBe(false)
    expect(paneVisible('workspace')).toBe(true)
    expect(paneVisible('plugin:pane')).toBe(true)
    expect(paneVisible('files')).toBe(true)
  })

  it('keeps the sibling plugin registered and in the same tree group throughout', () => {
    const contribution = registry.getArea('panes').find(pane => pane.id === 'plugin:pane')
    const pluginGroup = findGroupOfPane(currentTree(), 'plugin:pane')

    for (const filesOpen of [false, true, false, true]) {
      setFileBrowserOpen(filesOpen)

      expect(registry.getArea('panes').find(pane => pane.id === 'plugin:pane')).toBe(contribution)
      expect(findGroupOfPane(currentTree(), 'plugin:pane')).toBe(pluginGroup)
      expect(allPaneIds(currentTree())).toContain('plugin:pane')
      expect(paneVisible('plugin:pane')).toBe(true)
    }
  })
})
