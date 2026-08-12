import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ExcalidrawDocumentIdentity } from './identity'

describe('Excalidraw pane layout integration', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.resetModules()
  })

  afterEach(() => {
    vi.resetModules()
  })
  // Dynamic imports reload module-local pane registration and layout state for each test.

  async function setup() {
    const tree = await import('@/components/pane-shell/tree/store')
    const model = await import('@/components/pane-shell/tree/model')
    const { registry } = await import('@/contrib/registry')
    const drawings = await import('./store')

    registry.register({
      area: 'panes',
      data: { placement: 'main', uncloseable: true },
      id: 'workspace',
      render: () => null,
      title: 'chat'
    })
    tree.declareDefaultTree(model.group(['workspace'], { id: 'grp-main' }))
    tree.watchContributedPanes()

    return { drawings, model, tree }
  }

  it('opens half-width, preserves resized weights through × and restore, and tabs later drawings', async () => {
    const { drawings, model, tree } = await setup()
    const first: ExcalidrawDocumentIdentity = {
      path: '/drawings/design.excalidraw',
      profile: 'default',
      runtime: 'local'
    }
    const second: ExcalidrawDocumentIdentity = { ...first, path: '/drawings/flow.excalidraw' }
    const { excalidrawPaneId } = await import('./identity')
    const firstPaneId = excalidrawPaneId(first)
    const secondPaneId = excalidrawPaneId(second)

    drawings.openDrawing(first, 'fp1')

    const opened = tree.$layoutTree.get()!
    expect(opened).toMatchObject({ orientation: 'row', type: 'split', weights: [1, 1] })
    expect(model.findGroupOfPane(opened, firstPaneId)?.panes).toEqual([firstPaneId])

    if (opened.type !== 'split') throw new Error('expected Excalidraw to create a split')
    tree.setTreeSplitWeights(opened.id, [3, 2])
    tree.closeTreePane(firstPaneId)

    const minimized = tree.$layoutTree.get()!
    expect(model.findGroupOfPane(minimized, firstPaneId)?.minimized).toBe(true)
    expect(drawings.$excalidrawDocuments.get()).toHaveLength(1)

    tree.restoreTreePane(firstPaneId)
    const restored = tree.$layoutTree.get()!
    expect(model.findGroupOfPane(restored, firstPaneId)?.minimized).toBe(false)
    expect(restored.type === 'split' ? restored.weights : null).toEqual([3, 2])

    drawings.openDrawing(second, 'fp2')
    const stacked = tree.$layoutTree.get()!
    expect(model.findGroupOfPane(stacked, firstPaneId)?.panes).toEqual([firstPaneId, secondPaneId])
    expect(model.findGroupOfPane(stacked, secondPaneId)?.id).toBe(
      model.findGroupOfPane(stacked, firstPaneId)?.id
    )
  })
})
