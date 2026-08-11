import { beforeEach, describe, expect, it, vi } from 'vitest'

const { $narrowViewport, $registryVersion, register, registerPaneCloser, removeTreePane, revealTreePane } = vi.hoisted(() => ({
  $narrowViewport: { get: () => false, listen: () => () => undefined },
  $registryVersion: { get: () => 0, listen: () => () => undefined, subscribe: () => () => undefined },
  register: vi.fn(),
  registerPaneCloser: vi.fn(),
  removeTreePane: vi.fn(),
  revealTreePane: vi.fn()
}))

vi.mock('@/contrib/registry', () => ({ $registryVersion, registry: { getArea: () => [], register } }))
vi.mock('@/components/pane-shell/tree/store', () => ({ $narrowViewport, registerPaneCloser, removeTreePane, revealTreePane }))

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
        data: expect.objectContaining({ placement: 'right' }),
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

  it('removes a closed pane and closer so reopening registers one visible pane', () => {
    openDrawing(identity, 'fp1')
    const close = registerPaneCloser.mock.calls[0]?.[1]
    close?.()

    expect(removeTreePane).toHaveBeenCalledWith(excalidrawPaneId(identity))
    expect(registerPaneCloser).toHaveBeenLastCalledWith(excalidrawPaneId(identity))
    expect($excalidrawDocuments.get()).toEqual([])

    openDrawing(identity, 'fp2')
    expect(register).toHaveBeenCalledTimes(2)
    expect(revealTreePane).toHaveBeenLastCalledWith(excalidrawPaneId(identity))
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

  it('uses the registered closer confirmation to veto or discard unresolved work', async () => {
    const controller = { reconcileExternalChange: vi.fn(), waitForSave: vi.fn(), canCloseCleanly: vi.fn().mockReturnValue(false) }
    const confirm = vi.fn().mockReturnValueOnce(false).mockReturnValueOnce(true)
    vi.stubGlobal('confirm', confirm)
    openDrawing(identity, 'fp1')
    setDrawingController(identity, controller)
    const close = registerPaneCloser.mock.calls.at(-1)?.[1]

    close?.()
    await vi.waitFor(() => expect(confirm).toHaveBeenCalledTimes(1))
    expect($excalidrawDocuments.get()).toHaveLength(1)
    close?.()
    await vi.waitFor(() => expect($excalidrawDocuments.get()).toEqual([]))
    expect(confirm).toHaveBeenCalledTimes(2)
  })

  it('recovers a restored remote pane when its runtime reconnects', () => {
    const remoteIdentity = { ...identity, runtime: 'remote:host-a' }
    resetExcalidrawDocumentsForTest({ availableRuntimes: [], documents: [{ fingerprint: 'fp1', identity: remoteIdentity, status: 'connected' }] })

    restoreExcalidrawDocuments(['remote:host-a'])
    expect($excalidrawDocuments.get()).toEqual([expect.objectContaining({ identity: remoteIdentity, status: 'connected' })])
  })
})
