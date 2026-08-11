import { describe, expect, it } from 'vitest'

import { focusedDrawingPaths } from './focus-bridge'
import { excalidrawPaneId } from './identity'

const documents = [
  {
    fingerprint: 'one',
    identity: { path: '/drawings/one.excalidraw', profile: 'default', runtime: 'local' },
    status: 'connected' as const
  },
  {
    fingerprint: 'two',
    identity: { path: '/drawings/two.excalidraw', profile: 'default', runtime: 'local' },
    status: 'connected' as const
  },
  {
    fingerprint: 'other',
    identity: { path: '/drawings/other.excalidraw', profile: 'work', runtime: 'local' },
    status: 'connected' as const
  }
]

function layout(active: string, panes = documents) {
  return { active, id: 'drawings', panes: panes.map(document => excalidrawPaneId(document.identity)), type: 'group' as const }
}

describe('focused Excalidraw drawing bridge', () => {
  it('sends exactly the active connected drawing for its profile', () => {
    expect(focusedDrawingPaths(documents, 'default', layout(excalidrawPaneId(documents[0].identity)))).toEqual([
      '/drawings/one.excalidraw'
    ])
  })


  it('keeps multiple active drawing panes ambiguous', () => {
    const other = {
      active: excalidrawPaneId(documents[1].identity),
      id: 'other',
      panes: [excalidrawPaneId(documents[1].identity)],
      type: 'group' as const
    }

    const split = {
      children: [layout(excalidrawPaneId(documents[0].identity), [documents[0]]), other],
      id: 'split',
      orientation: 'row' as const,
      type: 'split' as const,
      weights: [1, 1]
    }

    expect(focusedDrawingPaths(documents, 'default', split)).toEqual(['/drawings/one.excalidraw', '/drawings/two.excalidraw'])
  })
  it('clears stale focus and isolates profiles', () => {
    expect(focusedDrawingPaths(documents, 'default', layout('workspace'))).toEqual([])
    expect(focusedDrawingPaths(documents, 'work', layout(excalidrawPaneId(documents[0].identity)))).toEqual([])
  })
})
