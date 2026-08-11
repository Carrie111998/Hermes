import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const bridge = vi.hoisted(() => {
  const atom = <T>(initial: T) => {
    let value = initial
    const listeners = new Set<() => void>()

    return {
      get: () => value,
      listen: (listener: () => void) => {
        listeners.add(listener)
        return () => listeners.delete(listener)
      },
      set: (next: T) => {
        value = next
        listeners.forEach(listener => listener())
      }
    }
  }

  return {
    $excalidrawDocuments: atom<unknown>([]),
    $layoutTree: atom<unknown>(null),
    activeSessionId: atom<string | null>(null),
    gateway: atom('open'),
    profile: atom('default'),
    request: vi.fn()
  }
})

vi.mock('@/components/pane-shell/tree/store', () => ({ $layoutTree: bridge.$layoutTree }))
vi.mock('@/sdk/index', () => ({
  host: {
    request: bridge.request,
    state: {
      activeSessionId: bridge.activeSessionId,
      gateway: bridge.gateway,
      profile: bridge.profile
    }
  }
}))
vi.mock('./store', () => ({ $excalidrawDocuments: bridge.$excalidrawDocuments }))

import { focusedDrawingPaths, installFocusedDrawingBridge } from './focus-bridge'
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
  return {
    active,
    id: 'drawings',
    panes: panes.map(document => excalidrawPaneId(document.identity)),
    type: 'group' as const
  }
}

let dispose: (() => void) | undefined

const settleBridge = async () => {
  await Promise.resolve()
  await Promise.resolve()
  await Promise.resolve()
}

const focusRequest = (sessionId: string, paths: string[]) => ({
  paths,
  profile: 'default',
  session_id: sessionId
})

beforeEach(() => {
  bridge.request.mockReset()
  bridge.request.mockResolvedValue(undefined)
  bridge.$excalidrawDocuments.set(documents)
  bridge.$layoutTree.set(layout(excalidrawPaneId(documents[0].identity)))
  bridge.activeSessionId.set(null)
  bridge.gateway.set('open')
  bridge.profile.set('default')
})

afterEach(() => {
  dispose?.()
  dispose = undefined
})

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

    expect(focusedDrawingPaths(documents, 'default', split)).toEqual([
      '/drawings/one.excalidraw',
      '/drawings/two.excalidraw'
    ])
  })
  it('clears stale focus and isolates profiles', () => {
    expect(focusedDrawingPaths(documents, 'default', layout('workspace'))).toEqual([])
    expect(focusedDrawingPaths(documents, 'work', layout(excalidrawPaneId(documents[0].identity)))).toEqual([])
  })
  it('clears session A before publishing focus for session B', async () => {
    bridge.activeSessionId.set('session-A')
    dispose = installFocusedDrawingBridge()
    await settleBridge()

    bridge.activeSessionId.set('session-B')
    await settleBridge()

    expect(bridge.request).toHaveBeenNthCalledWith(
      1,
      'excalidraw.focus',
      focusRequest('session-A', ['/drawings/one.excalidraw'])
    )
    expect(bridge.request).toHaveBeenNthCalledWith(2, 'excalidraw.focus', focusRequest('session-A', []))
    expect(bridge.request).toHaveBeenNthCalledWith(
      3,
      'excalidraw.focus',
      focusRequest('session-B', ['/drawings/one.excalidraw'])
    )
  })

  it('clears the prior session when the active session becomes empty', async () => {
    bridge.activeSessionId.set('session-A')
    dispose = installFocusedDrawingBridge()
    await settleBridge()

    bridge.activeSessionId.set(null)
    await settleBridge()

    expect(bridge.request).toHaveBeenNthCalledWith(
      1,
      'excalidraw.focus',
      focusRequest('session-A', ['/drawings/one.excalidraw'])
    )
    expect(bridge.request).toHaveBeenNthCalledWith(2, 'excalidraw.focus', focusRequest('session-A', []))
    expect(bridge.request).toHaveBeenCalledTimes(2)
  })

  it('clears the prior session when disposed', async () => {
    bridge.activeSessionId.set('session-A')
    dispose = installFocusedDrawingBridge()
    await settleBridge()

    dispose()
    dispose = undefined

    expect(bridge.request).toHaveBeenNthCalledWith(
      1,
      'excalidraw.focus',
      focusRequest('session-A', ['/drawings/one.excalidraw'])
    )
    expect(bridge.request).toHaveBeenNthCalledWith(2, 'excalidraw.focus', focusRequest('session-A', []))
    expect(bridge.request).toHaveBeenCalledTimes(2)
  })

  it('does not let an old request completion clear session B focus', async () => {
    let resolveSessionAFocus!: () => void
    let resolveSessionAClear!: () => void
    const sessionAFocus = new Promise<void>(resolve => {
      resolveSessionAFocus = resolve
    })
    const sessionAClear = new Promise<void>(resolve => {
      resolveSessionAClear = resolve
    })

    bridge.request.mockImplementationOnce(() => sessionAFocus)
    bridge.request.mockImplementationOnce(() => sessionAClear)
    bridge.request.mockResolvedValueOnce(undefined)
    bridge.activeSessionId.set('session-A')
    dispose = installFocusedDrawingBridge()
    await settleBridge()

    bridge.activeSessionId.set('session-B')
    await settleBridge()

    expect(bridge.request).toHaveBeenNthCalledWith(
      1,
      'excalidraw.focus',
      focusRequest('session-A', ['/drawings/one.excalidraw'])
    )
    expect(bridge.request).toHaveBeenNthCalledWith(2, 'excalidraw.focus', focusRequest('session-A', []))

    resolveSessionAClear()
    await settleBridge()
    expect(bridge.request).toHaveBeenNthCalledWith(
      3,
      'excalidraw.focus',
      focusRequest('session-B', ['/drawings/one.excalidraw'])
    )

    resolveSessionAFocus()
    await settleBridge()

    expect(bridge.request).toHaveBeenCalledTimes(3)
    expect(bridge.request).not.toHaveBeenCalledWith('excalidraw.focus', focusRequest('session-B', []))
  })
})
