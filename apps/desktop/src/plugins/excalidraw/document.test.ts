import { beforeEach, describe, expect, it, vi } from 'vitest'

const { isDesktopFsRemoteMode, readDesktopFileText, selectDesktopPaths, writeDesktopFileText } = vi.hoisted(() => ({
  isDesktopFsRemoteMode: vi.fn(),
  readDesktopFileText: vi.fn(),
  selectDesktopPaths: vi.fn(),
  writeDesktopFileText: vi.fn()
}))

vi.mock('@/lib/desktop-fs', () => ({ isDesktopFsRemoteMode, readDesktopFileText, selectDesktopPaths, writeDesktopFileText }))

import { createDrawingController, loadDrawing } from './document'
import type { ExcalidrawDocumentIdentity } from './identity'

const identity: ExcalidrawDocumentIdentity = { path: '/drawings/design.excalidraw', profile: 'default', runtime: 'local' }
const source = JSON.stringify({ type: 'excalidraw', version: 2, elements: [{ id: 'source' }], appState: {}, files: {}, custom: 'kept' })

beforeEach(() => {
  vi.useFakeTimers()
  readDesktopFileText.mockReset()
  selectDesktopPaths.mockReset()
  writeDesktopFileText.mockReset()
  isDesktopFsRemoteMode.mockReturnValue(false)
  writeDesktopFileText.mockResolvedValue({ path: identity.path })
})

describe('Excalidraw documents', () => {
  it('uses desktop-fs and does not rewrite an invalid document', async () => {
    readDesktopFileText.mockResolvedValue({ path: identity.path, text: '{bad json}', byteSize: 9 })

    await expect(loadDrawing(identity)).rejects.toThrow('Invalid Excalidraw document')
    expect(writeDesktopFileText).not.toHaveBeenCalled()
  })

  it('requires the Excalidraw root contract while defaulting omitted optional envelopes', async () => {
    const validSkillEnvelope = JSON.stringify({ type: 'excalidraw', version: 2, elements: [], skill: 'created' })
    readDesktopFileText.mockResolvedValue({ path: identity.path, text: validSkillEnvelope, byteSize: validSkillEnvelope.length })

    await expect(loadDrawing(identity)).resolves.toMatchObject({
      appState: {},
      elements: [],
      envelope: { skill: 'created', type: 'excalidraw', version: 2 },
      files: {}
    })

    for (const invalid of [
      {},
      { type: 'wrong', version: 2, elements: [] },
      { type: 'excalidraw', version: 1, elements: [] },
      { type: 'excalidraw', version: 2, elements: {} },
      { type: 'excalidraw', version: 2, elements: [], appState: [] },
      { type: 'excalidraw', version: 2, elements: [], files: [] }
    ]) {
      const text = JSON.stringify(invalid)
      readDesktopFileText.mockResolvedValue({ path: identity.path, text, byteSize: text.length })
      await expect(loadDrawing(identity)).rejects.toThrow('Invalid Excalidraw document')
    }
  })

  it('preserves the full document envelope and unknown keys when saving', async () => {
    readDesktopFileText.mockResolvedValue({ path: identity.path, text: source, byteSize: source.length })
    const drawing = await loadDrawing(identity)
    const controller = createDrawingController(drawing)

    controller.onSceneChange([{ id: 'updated', unknownElementKey: 'kept' }], { theme: 'dark' })
    await vi.runAllTimersAsync()

    expect(JSON.parse(writeDesktopFileText.mock.calls[0][1])).toMatchObject({
      custom: 'kept',
      elements: [{ id: 'updated', unknownElementKey: 'kept' }],
      appState: { theme: 'dark' },
      files: {}
    })
  })

  it('persists files received from the editor scene change', async () => {
    readDesktopFileText.mockResolvedValue({ path: identity.path, text: source, byteSize: source.length })
    const controller = createDrawingController(await loadDrawing(identity))

    controller.onSceneChange([{ id: 'updated' }], {}, { image: { dataURL: 'data:image/png;base64,AA==' } })
    await vi.runAllTimersAsync()

    expect(JSON.parse(writeDesktopFileText.mock.calls[0][1]).files).toEqual({ image: { dataURL: 'data:image/png;base64,AA==' } })
  })

  it('serializes saves and writes the newest pending scene', async () => {
    readDesktopFileText
      .mockResolvedValueOnce({ path: identity.path, text: source, byteSize: source.length })
      .mockResolvedValueOnce({ path: identity.path, text: source, byteSize: source.length })
      .mockResolvedValueOnce({ path: identity.path, text: JSON.stringify({ ...JSON.parse(source), elements: [{ id: 'first' }] }), byteSize: source.length })
    const controller = createDrawingController(await loadDrawing(identity))
    let resolveFirstWrite!: (value: { path: string }) => void

    const firstWrite = new Promise<{ path: string }>(resolve => {
      resolveFirstWrite = resolve
    })

    writeDesktopFileText.mockImplementationOnce(() => firstWrite)

    controller.onSceneChange([{ id: 'first' }], {})
    await vi.runAllTimersAsync()
    controller.onSceneChange([{ id: 'second' }], {})
    resolveFirstWrite!({ path: identity.path })
    await vi.runAllTimersAsync()

    expect(writeDesktopFileText).toHaveBeenLastCalledWith(identity.path, expect.stringContaining('second'))
  })

  it('conflicts on a changed baseline and can reload or explicitly keep the pane version', async () => {
    readDesktopFileText
      .mockResolvedValueOnce({ path: identity.path, text: source, byteSize: source.length })
      .mockResolvedValueOnce({ path: identity.path, text: JSON.stringify({ ...JSON.parse(source), elements: [{ id: 'disk' }] }), byteSize: 1 })
      .mockResolvedValueOnce({ path: identity.path, text: JSON.stringify({ ...JSON.parse(source), elements: [{ id: 'disk' }] }), byteSize: 1 })
      .mockResolvedValueOnce({ path: identity.path, text: JSON.stringify({ ...JSON.parse(source), elements: [{ id: 'disk' }] }), byteSize: 1 })
    const controller = createDrawingController(await loadDrawing(identity))

    controller.onSceneChange([{ id: 'pane' }], {})
    await vi.runAllTimersAsync()
    expect(controller.getState().status).toBe('conflict')

    await controller.keepPaneVersion()
    expect(writeDesktopFileText).toHaveBeenCalledWith(identity.path, expect.stringContaining('pane'))

    await controller.reload()
    expect(controller.getState().elements).toEqual([{ id: 'disk' }])
  })

  it('retains edits after errors and exposes retry and Save As through desktop-fs', async () => {
    readDesktopFileText.mockResolvedValue({ path: identity.path, text: source, byteSize: source.length })
    writeDesktopFileText.mockRejectedValueOnce(new Error('offline'))
    const controller = createDrawingController(await loadDrawing(identity))

    controller.onSceneChange([{ id: 'pane' }], {})
    await vi.runAllTimersAsync()
    expect(controller.getState()).toMatchObject({ status: 'error', elements: [{ id: 'pane' }] })

    await controller.retry()
    expect(writeDesktopFileText).toHaveBeenLastCalledWith(identity.path, expect.stringContaining('pane'))

    selectDesktopPaths.mockResolvedValue(['/drawings/copy.excalidraw'])
    await expect(controller.saveAs()).resolves.toEqual({ ...identity, path: '/drawings/copy.excalidraw' })
    expect(selectDesktopPaths).toHaveBeenCalledWith(expect.objectContaining({ multiple: false }))
    expect(writeDesktopFileText).toHaveBeenLastCalledWith('/drawings/copy.excalidraw', expect.stringContaining('pane'))
    expect(controller.getState().identity).toEqual(identity)
  })

  it('reloads clean external replacements and conflicts dirty changes without writing', async () => {
    const replacement = JSON.stringify({ ...JSON.parse(source), elements: [{ id: 'external' }] })
    readDesktopFileText.mockResolvedValueOnce({ path: identity.path, text: source, byteSize: source.length }).mockResolvedValueOnce({ path: identity.path, text: replacement, byteSize: replacement.length })
    const controller = createDrawingController(await loadDrawing(identity))
    let externalNotifications = 0
    controller.subscribe(origin => {
      if (origin === 'external') {externalNotifications += 1}
    })

    await controller.reconcileExternalChange('external-fingerprint')
    expect(controller.getState()).toMatchObject({ elements: [{ id: 'external' }], fingerprint: replacement, status: 'ready' })
    expect(externalNotifications).toBe(1)

    controller.onSceneChange([{ id: 'pane' }], {})
    await controller.reconcileExternalChange('newer-fingerprint')
    expect(controller.getState().status).toBe('conflict')
    expect(writeDesktopFileText).not.toHaveBeenCalled()
  })

  it('waits for a pending save and refuses close after a conflict', async () => {
    readDesktopFileText.mockResolvedValue({ path: identity.path, text: source, byteSize: source.length })
    const controller = createDrawingController(await loadDrawing(identity))
    let resolveWrite!: (value: { path: string }) => void

    const write = new Promise<{ path: string }>(resolve => {
      resolveWrite = resolve
    })

    writeDesktopFileText.mockReturnValueOnce(write)

    controller.onSceneChange([{ id: 'pane' }], {})
    const closing = controller.waitForSave()
    expect(controller.canCloseCleanly()).toBe(false)
    resolveWrite!({ path: identity.path })
    await closing
    expect(controller.canCloseCleanly()).toBe(true)

    controller.onSceneChange([{ id: 'pane' }], {})
    await controller.reconcileExternalChange('conflicting')
    expect(controller.canCloseCleanly()).toBe(false)
  })
})
