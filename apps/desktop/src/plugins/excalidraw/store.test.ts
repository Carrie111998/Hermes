import { beforeEach, describe, expect, it, vi } from 'vitest'

const {
  $narrowViewport,
  $registryVersion,
  register,
  registerPaneCloser,
  removeTreePane,
  revealTreePane,
  setPaneCollapsed
} = vi.hoisted(() => ({
  $narrowViewport: { get: () => false, listen: () => () => undefined },
  $registryVersion: { get: () => 0, listen: () => () => undefined, subscribe: () => () => undefined },
  register: vi.fn(),
  registerPaneCloser: vi.fn(),
  removeTreePane: vi.fn(),
  revealTreePane: vi.fn(),
  setPaneCollapsed: vi.fn()
}))

vi.mock('@/contrib/registry', () => ({ $registryVersion, registry: { getArea: () => [], register } }))
vi.mock('@/components/pane-shell/tree/store', () => ({
  $narrowViewport,
  registerPaneCloser,
  removeTreePane,
  revealTreePane,
  setPaneCollapsed
}))

import { type ExcalidrawDocumentIdentity, excalidrawPaneId } from './identity'
import { $excalidrawDocuments, handleChangedDocument, openDrawing, requestDrawingClose, resetExcalidrawDocumentsForTest, restoreExcalidrawDocuments, setDrawingController } from './store'

describe('Excalidraw drawing panes', () => {
  const identity: ExcalidrawDocumentIdentity = {
    path: '/drawings/design.excalidraw',
    profile: 'default',
    runtime: 'local'
  }

  beforeEach(() => {
    register.mockReset()
    register.mockReturnValue(() => undefined)
    revealTreePane.mockReset()
    registerPaneCloser.mockReset()
    removeTreePane.mockReset()
    setPaneCollapsed.mockReset()
    resetExcalidrawDocumentsForTest()
  })

  it('focuses an existing pane and registers a new right-side pane', () => {
    openDrawing(identity, 'fp1')
    openDrawing(identity, 'fp2')

    const paneId = excalidrawPaneId(identity)
    expect(register).toHaveBeenCalledTimes(1)
    expect(registerPaneCloser).toHaveBeenCalledWith(paneId, expect.any(Function))
    expect(revealTreePane).toHaveBeenCalledWith(paneId)
    expect(register).toHaveBeenCalledWith(
      expect.objectContaining({
        area: 'panes',
        data: expect.objectContaining({
          dock: { pane: 'workspace', pos: 'right' },
          placement: 'right'
        }),
        id: paneId,
        title: 'design.excalidraw'
      })
    )
    expect($excalidrawDocuments.get()).toEqual([
      expect.objectContaining({ fingerprint: 'fp2', identity, status: 'connected' })
    ])
  })

  it('registers restored remote panes before marking unavailable identities disconnected', () => {
    const remoteIdentity: ExcalidrawDocumentIdentity = {
      path: '/drawings/remote.excalidraw',
      profile: 'remote-profile',
      runtime: 'remote:host-a'
    }

    resetExcalidrawDocumentsForTest({
      availableRuntimes: [],
      documents: [{ fingerprint: 'fp1', identity: remoteIdentity, status: 'connected' }]
    })

    expect(register).toHaveBeenCalledWith(expect.objectContaining({ id: excalidrawPaneId(remoteIdentity) }))
    expect($excalidrawDocuments.get()).toEqual([
      expect.objectContaining({ identity: remoteIdentity, status: 'disconnected' })
    ])
  })

  it('minimizes the pane without forgetting its document or registration', () => {
    openDrawing(identity, 'fp1')
    const paneId = excalidrawPaneId(identity)
    const close = registerPaneCloser.mock.calls[0]?.[1]

    close?.()

    expect(setPaneCollapsed).toHaveBeenCalledWith(paneId, true)
    expect(removeTreePane).not.toHaveBeenCalled()
    expect(register).toHaveBeenCalledTimes(1)
    expect($excalidrawDocuments.get()).toEqual([
      expect.objectContaining({ fingerprint: 'fp1', identity, status: 'connected' })
    ])

    openDrawing(identity, 'fp2')
    expect(register).toHaveBeenCalledTimes(1)
    expect(revealTreePane).toHaveBeenLastCalledWith(paneId)
  })

  it('reconciles only the matching full identity', async () => {
    const samePathElsewhere = { ...identity, runtime: 'remote:host-a' }
    const controller = { reconcileExternalChange: vi.fn(), waitForSave: vi.fn(), canCloseCleanly: vi.fn() }
    openDrawing(identity, 'fp1')
    openDrawing(samePathElsewhere, 'fp2')
    setDrawingController(identity, controller)

    await handleChangedDocument(samePathElsewhere, 'fp3')
    expect(controller.reconcileExternalChange).not.toHaveBeenCalled()

    await handleChangedDocument(identity, 'fp4')
    expect(controller.reconcileExternalChange).toHaveBeenCalledWith('fp4')
  })

  it('waits for saves and keeps conflict panes open unless discard is confirmed', async () => {
    const controller = { reconcileExternalChange: vi.fn(), waitForSave: vi.fn(), canCloseCleanly: vi.fn() }
    openDrawing(identity, 'fp1')
    setDrawingController(identity, controller)
    controller.canCloseCleanly.mockReturnValueOnce(true)
    await expect(requestDrawingClose(identity)).resolves.toBe(true)

    setDrawingController(identity, controller)
    openDrawing(identity, 'fp1')
    controller.canCloseCleanly.mockReturnValue(false)
    await expect(requestDrawingClose(identity, () => false)).resolves.toBe(false)
    expect($excalidrawDocuments.get()).toHaveLength(1)
    await expect(requestDrawingClose(identity, () => true)).resolves.toBe(true)
    expect(controller.waitForSave).toHaveBeenCalledTimes(3)
    expect($excalidrawDocuments.get()).toEqual([])
  })

  it('docks the first drawing beside workspace and later drawings into its tab group', () => {
    const second = { ...identity, path: '/drawings/flow.excalidraw' }
    const firstPaneId = excalidrawPaneId(identity)

    openDrawing(identity, 'fp1')
    openDrawing(second, 'fp2')

    expect(register).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({
        data: {
          dock: { pane: firstPaneId, pos: 'center' },
          placement: 'right'
        },
        id: excalidrawPaneId(second)
      })
    )
  })

  it('recovers a restored remote pane when its runtime reconnects', () => {
    const remoteIdentity = { ...identity, runtime: 'remote:host-a' }
    resetExcalidrawDocumentsForTest({ availableRuntimes: [], documents: [{ fingerprint: 'fp1', identity: remoteIdentity, status: 'connected' }] })

    restoreExcalidrawDocuments(['remote:host-a'])
    expect($excalidrawDocuments.get()).toEqual([expect.objectContaining({ identity: remoteIdentity, status: 'connected' })])
  })
})
