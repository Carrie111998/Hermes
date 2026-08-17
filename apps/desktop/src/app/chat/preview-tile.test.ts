import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Preview tiles mirror $visiblePreviewTabs into layout-tree panes: only the
// ACTIVE session's tabs (plus pins) become panes, so switching sessions swaps
// the drawer, and pinning a tab surfaces it in every session.

describe('preview tiles mirror the visible session tabs', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.resetModules()
  })

  afterEach(() => {
    vi.resetModules()
  })

  async function setup() {
    const preview = await import('@/store/preview')
    const session = await import('@/store/session')
    const tree = await import('@/components/pane-shell/tree/store')
    const model = await import('@/components/pane-shell/tree/model')
    const { registry } = await import('@/contrib/registry')
    const { watchPreviewTiles } = await import('./preview-tile')

    registry.register({
      area: 'panes',
      data: { placement: 'main', uncloseable: true },
      id: 'workspace',
      render: () => null,
      title: 'workspace'
    })
    tree.declareDefaultTree(model.group(['workspace'], { active: 'workspace', id: 'grp-main' }))
    // The app root wires registry changes into the tree; mirror it here.
    tree.watchContributedPanes()
    watchPreviewTiles()

    return { model, preview, session, tree }
  }

  it('renders only the active session previews, pinning spans sessions', async () => {
    const { preview, session, tree } = await setup()

    session.$selectedStoredSessionId.set('sess-1')
    preview.openPreview(
      {
        kind: 'file',
        label: 'a.html',
        path: '/work/a.html',
        previewKind: 'html',
        source: '/work/a.html',
        url: 'file:///work/a.html'
      },
      'tool-result'
    )

    expect(tree.treePanesWithPrefix('preview-tile:')).toHaveLength(1)

    // Switching sessions hides the pane (the tab stays open in the store).
    session.$selectedStoredSessionId.set('sess-2')
    expect(tree.treePanesWithPrefix('preview-tile:')).toHaveLength(0)
    expect(preview.$previewTabs.get()).toHaveLength(1)

    // Pinning makes it visible again in the new session.
    preview.setPreviewTabPinned(preview.$previewTabs.get()[0]!.id, true)
    expect(tree.treePanesWithPrefix('preview-tile:')).toHaveLength(1)

    // And closing the tab removes the pane for good.
    preview.closeRightRailTab(preview.$previewTabs.get()[0]!.id)
    expect(tree.treePanesWithPrefix('preview-tile:')).toHaveLength(0)
  })

  it('does not create panes for another session tabs', async () => {
    const { preview, session, tree } = await setup()

    session.$selectedStoredSessionId.set('sess-1')
    preview.openPreview(
      {
        kind: 'file',
        label: 'a.html',
        path: '/work/a.html',
        previewKind: 'html',
        source: '/work/a.html',
        url: 'file:///work/a.html'
      },
      'tool-result'
    )

    session.$selectedStoredSessionId.set('sess-2')
    preview.openPreview(
      {
        kind: 'file',
        label: 'b.html',
        path: '/work/b.html',
        previewKind: 'html',
        source: '/work/b.html',
        url: 'file:///work/b.html'
      },
      'tool-result'
    )

    expect(tree.treePanesWithPrefix('preview-tile:')).toHaveLength(1)

    session.$selectedStoredSessionId.set('sess-1')
    expect(tree.treePanesWithPrefix('preview-tile:')).toHaveLength(1)
  })
})
