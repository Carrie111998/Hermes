import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { group, split } from '@/components/pane-shell/tree/model'
import { $hiddenTreePanes, $layoutTree, noteActiveTreeGroup } from '@/components/pane-shell/tree/store'
import { registry } from '@/contrib/registry'
import { $rightRailActiveTabId } from '@/store/layout'
import { $previewTabs, closeRightRail, openPreview, type PreviewTarget } from '@/store/preview'
import { $rightPanelOpen } from '@/store/right-panel'

import { closeActiveTab } from './close-tab'

function fileTarget(path: string): PreviewTarget {
  return {
    kind: 'file',
    label: path,
    path,
    previewKind: 'text',
    source: path,
    url: `file://${path}`
  }
}

describe('closeActiveTab', () => {
  beforeEach(() => {
    vi.stubGlobal('document', { activeElement: null })
    registry.register({
      area: 'panes',
      data: { placement: 'main', uncloseable: true },
      id: 'workspace',
      render: () => null,
      title: 'Workspace'
    })
    $layoutTree.set(
      split(
        'row',
        [
          group(['workspace'], { id: 'grp-main' }),
          group(['files', 'preview', 'terminal'], { active: 'preview', id: 'grp-right-tools' })
        ],
        [3, 1]
      )
    )
    $hiddenTreePanes.set(new Set())
    $rightPanelOpen.set(true)
    noteActiveTreeGroup('grp-right-tools')
    closeRightRail()
    window.localStorage.clear()
  })

  it('does not close a hidden Preview child while the workspace owns focus', () => {
    openPreview(fileTarget('/work/notes.md'), 'manual')
    noteActiveTreeGroup('grp-main')

    expect(closeActiveTab()).toBe(false)
    expect($previewTabs.get()).toHaveLength(1)
  })

  it('closes the active Preview child after a user moves Preview into its own group', () => {
    $layoutTree.set(
      split(
        'row',
        [
          group(['workspace'], { id: 'grp-main' }),
          group(['files'], { id: 'grp-files' }),
          group(['preview'], { id: 'grp-user-preview' })
        ],
        [3, 1, 1]
      )
    )
    openPreview(fileTarget('/work/notes.md'), 'manual')
    noteActiveTreeGroup('grp-user-preview')

    expect(closeActiveTab()).toBe(true)
    expect($previewTabs.get()).toHaveLength(0)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    closeRightRail()
    window.localStorage.clear()
  })

  it('closes the active file preview tab (⌘W happy path)', () => {
    openPreview(fileTarget('/work/notes.md'), 'manual')

    expect($previewTabs.get()).toHaveLength(1)
    expect($rightRailActiveTabId.get()).toBe('file:file:///work/notes.md')

    expect(closeActiveTab()).toBe(true)
    expect($previewTabs.get()).toHaveLength(0)
  })

  it('closes the visible tab when the active selection points at a tab that is gone', () => {
    // The rail falls back to tabs[0] until React syncs the selection, so ⌘W has
    // to act on what is actually on screen rather than no-op'ing.
    openPreview(fileTarget('/work/notes.md'), 'manual')
    $rightRailActiveTabId.set('file:file:///work/stale.md')

    expect($previewTabs.get()).toHaveLength(1)
    expect(closeActiveTab()).toBe(true)
    expect($previewTabs.get()).toHaveLength(0)
  })
})
