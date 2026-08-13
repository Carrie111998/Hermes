import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ExcalidrawDocumentIdentity } from './identity'

const first: ExcalidrawDocumentIdentity = {
  path: '/drawings/design.excalidraw',
  profile: 'default',
  runtime: 'local'
}
const second: ExcalidrawDocumentIdentity = { ...first, path: '/drawings/flow.excalidraw' }

describe('Excalidraw pane layout integration', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.resetModules()
  })

  afterEach(() => {
    vi.resetModules()
  })

  // Dynamic imports are required because each test resets module-local pane registration and layout state.

  async function setup() {
    const tree = await import('@/components/pane-shell/tree/store')
    const model = await import('@/components/pane-shell/tree/model')
    const { registry } = await import('@/contrib/registry')
    const drawings = await import('./store')

    for (const [id, data] of [
      ['sessions', { placement: 'left' }],
      ['workspace', { placement: 'main', uncloseable: true }],
      ['review', { placement: 'right' }],
      ['files', { placement: 'right' }],
      ['terminal', { placement: 'bottom' }]
    ] as const) {
      registry.register({ area: 'panes', data, id, render: () => null, title: id })
    }

    tree.declareDefaultTree(
      model.split(
        'row',
        [
          model.group(['sessions'], { id: 'grp-sessions' }),
          model.group(['workspace'], { id: 'grp-main' }),
          model.split(
            'column',
            [
              model.split(
                'row',
                [model.group(['review'], { id: 'grp-review' }), model.group(['files'], { id: 'grp-files' })],
                [1, 1.2],
                'spl-rail'
              ),
              model.group(['terminal'], { id: 'grp-terminal' })
            ],
            [1.6, 1],
            'spl-right'
          )
        ],
        [1, 3.4, 1.25],
        'spl-root'
      )
    )
    tree.watchContributedPanes()

    return { drawings, model, tree }
  }

  it('minimizes a drawing zone without closing its document and preserves resized weights', async () => {
    const { drawings, model, tree } = await setup()
    const { excalidrawPaneId } = await import('./identity')
    const firstPaneId = excalidrawPaneId(first)
    const secondPaneId = excalidrawPaneId(second)

    drawings.openDrawing(first, 'fp1')

    const opened = tree.$layoutTree.get()!
    expect(opened).toMatchObject({ id: 'spl-root', orientation: 'row', type: 'split' })
    if (opened.type !== 'split') throw new Error('expected root drawing split')
    const drawingIndex = opened.children.findIndex(child => model.allPaneIds(child).includes(firstPaneId))
    const drawingWeight = opened.weights[drawingIndex]
    const existingWeight = opened.weights.reduce((sum, weight, index) => (index === drawingIndex ? sum : sum + weight), 0)
    expect(drawingIndex).toBe(opened.children.length - 1)
    expect(drawingWeight / (drawingWeight + existingWeight)).toBeCloseTo(0.5)
    expect(opened.weights.slice(0, drawingIndex)).toEqual([1, 3.4, 1.25].map(weight => weight / 5.65))
    expect(model.findGroupOfPane(opened, firstPaneId)?.panes).toEqual([firstPaneId])

    const resizedWeights = [0.2, 0.7, 0.3, 2]
    tree.setTreeSplitWeights(opened.id, resizedWeights)
    tree.collapseTreePane(firstPaneId)

    const minimized = tree.$layoutTree.get()!
    expect(model.findGroupOfPane(minimized, firstPaneId)?.minimized).toBe(true)
    expect(drawings.$excalidrawDocuments.get()).toHaveLength(1)

    drawings.openDrawing(first, 'fp2')
    const restored = tree.$layoutTree.get()!
    expect(model.findGroupOfPane(restored, firstPaneId)).toMatchObject({ active: firstPaneId, minimized: false })
    expect(restored.type === 'split' ? restored.weights : null).toEqual(resizedWeights)

    drawings.openDrawing(second, 'fp3')
    const stacked = tree.$layoutTree.get()!
    expect(model.findGroupOfPane(stacked, firstPaneId)?.panes).toEqual([firstPaneId, secondPaneId])
    expect(model.findGroupOfPane(stacked, secondPaneId)?.id).toBe(
      model.findGroupOfPane(stacked, firstPaneId)?.id
    )
  })

  it('closes only the requested drawing tab and leaves its active sibling visible', async () => {
    const { drawings, model, tree } = await setup()
    const { excalidrawPaneId } = await import('./identity')
    const firstPaneId = excalidrawPaneId(first)
    const secondPaneId = excalidrawPaneId(second)

    drawings.openDrawing(first, 'fp1')
    drawings.openDrawing(second, 'fp2')
    drawings.setDrawingController(first, {
      canCloseCleanly: vi.fn().mockReturnValue(true),
      reconcileExternalChange: vi.fn(),
      waitForSave: vi.fn().mockResolvedValue(undefined)
    })
    tree.closeTreePane(firstPaneId)

    await vi.waitFor(() => expect(model.findGroupOfPane(tree.$layoutTree.get()!, firstPaneId)).toBeNull())
    const siblingGroup = model.findGroupOfPane(tree.$layoutTree.get()!, secondPaneId)
    expect(siblingGroup).toMatchObject({
      active: secondPaneId,
      panes: [secondPaneId]
    })
    expect(siblingGroup?.minimized).not.toBe(true)
    expect(drawings.$excalidrawDocuments.get()).toEqual([
      expect.objectContaining({ identity: second })
    ])
  })

  it('keeps a persisted custom drawing zone authoritative during restoration', async () => {
    const { excalidrawPaneId } = await import('./identity')
    const firstPaneId = excalidrawPaneId(first)
    const secondPaneId = excalidrawPaneId(second)
    window.localStorage.setItem(
      'hermes.desktop.layoutTree.v2',
      JSON.stringify({
        children: [
          { active: 'sessions', id: 'custom-sessions', panes: ['sessions'], type: 'group' },
          { active: 'workspace', id: 'custom-main', panes: ['workspace'], type: 'group' },
          {
            active: firstPaneId,
            id: 'custom-drawings',
            minimized: true,
            panes: [firstPaneId, secondPaneId],
            type: 'group'
          },
          {
            children: [
              { active: 'review', id: 'custom-review', panes: ['review'], type: 'group' },
              { active: 'files', id: 'custom-files', panes: ['files'], type: 'group' }
            ],
            id: 'custom-rail',
            orientation: 'column',
            type: 'split',
            weights: [5, 2]
          },
          { active: 'terminal', id: 'custom-terminal', panes: ['terminal'], type: 'group' }
        ],
        id: 'custom-root',
        orientation: 'row',
        type: 'split',
        weights: [2, 5, 7, 3, 1]
      })
    )

    const { drawings, model, tree } = await setup()
    drawings.resetExcalidrawDocumentsForTest({
      availableRuntimes: ['local'],
      documents: [
        { fingerprint: 'fp-second', identity: second, status: 'connected' },
        { fingerprint: 'fp-first', identity: first, status: 'connected' }
      ]
    })

    const restored = tree.$layoutTree.get()!
    expect(restored).toMatchObject({ id: 'custom-root', weights: [2, 5, 7, 3, 1] })
    expect(model.findGroup(restored, 'custom-drawings')).toMatchObject({
      active: firstPaneId,
      minimized: true,
      panes: [firstPaneId, secondPaneId]
    })
  })

  it('re-registers a explicitly closed drawing into its surviving drawing group', async () => {
    const { drawings, model, tree } = await setup()
    const { excalidrawPaneId } = await import('./identity')
    const firstPaneId = excalidrawPaneId(first)
    const secondPaneId = excalidrawPaneId(second)

    drawings.openDrawing(first, 'fp1')
    drawings.openDrawing(second, 'fp2')
    drawings.closeDrawing(first)
    drawings.openDrawing(first, 'fp3')

    const reopenedGroup = model.findGroupOfPane(tree.$layoutTree.get()!, firstPaneId)
    expect(reopenedGroup?.panes).toEqual([secondPaneId, firstPaneId])
    expect(model.findGroupOfPane(tree.$layoutTree.get()!, secondPaneId)?.id).toBe(reopenedGroup?.id)
  })
})
