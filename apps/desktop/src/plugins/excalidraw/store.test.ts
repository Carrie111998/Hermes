import { beforeEach, describe, expect, it, vi } from 'vitest'

const {
  $narrowViewport,
  $registryVersion,
  register,
  registerPaneCloser,
  removeTreePane,
  revealTreePane
} = vi.hoisted(() => ({
  $narrowViewport: { get: () => false, listen: () => () => undefined },
  $registryVersion: { get: () => 0, listen: () => () => undefined, subscribe: () => () => undefined },
  register: vi.fn(),
  registerPaneCloser: vi.fn(),
  removeTreePane: vi.fn(),
  revealTreePane: vi.fn()
}))

vi.mock('@/contrib/registry', () => ({ $registryVersion, registry: { getArea: () => [], register } }))
vi.mock('@/components/pane-shell/tree/store', () => ({
  $narrowViewport,
  registerPaneCloser,
  removeTreePane,
  revealTreePane
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
          dock: { root: 'right' },
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

  it('routes tab Close through save-aware drawing removal', async () => {
    const controller = {
      reconcileExternalChange: vi.fn(),
      waitForSave: vi.fn().mockResolvedValue(undefined),
      canCloseCleanly: vi.fn().mockReturnValue(true)
    }
    openDrawing(identity, 'fp1')
    setDrawingController(identity, controller)
    const paneId = excalidrawPaneId(identity)
    const close = registerPaneCloser.mock.calls[0]?.[1]

    close?.()

    await vi.waitFor(() => expect(removeTreePane).toHaveBeenCalledWith(paneId))
    expect(controller.waitForSave).toHaveBeenCalledTimes(1)
    expect(registerPaneCloser).toHaveBeenLastCalledWith(paneId)
    expect($excalidrawDocuments.get()).toEqual([])

    await handleChangedDocument(identity, 'fp2')
    expect(controller.reconcileExternalChange).not.toHaveBeenCalled()
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

  it('closes a pane that failed before its drawing controller mounted', async () => {
    openDrawing(identity, 'fp1')

    await expect(requestDrawingClose(identity)).resolves.toBe(true)

    expect(removeTreePane).toHaveBeenCalledWith(excalidrawPaneId(identity))
    expect($excalidrawDocuments.get()).toEqual([])
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
