import { beforeEach, describe, expect, it, vi } from 'vitest'

const {
  isDesktopFsRemoteMode,
  isDesktopFsWriteConflict,
  readDesktopDrawingFileText,
  selectDesktopPaths,
  writeDesktopDrawingFileText
} = vi.hoisted(() => ({
  isDesktopFsRemoteMode: vi.fn(),
  isDesktopFsWriteConflict: vi.fn(),
  readDesktopDrawingFileText: vi.fn(),
  selectDesktopPaths: vi.fn(),
  writeDesktopDrawingFileText: vi.fn()
}))

vi.mock('@/lib/desktop-fs', () => ({
  isDesktopFsRemoteMode,
  isDesktopFsWriteConflict,
  readDesktopDrawingFileText,
  selectDesktopPaths,
  writeDesktopDrawingFileText
}))

import { createDrawingController, loadDrawing } from './document'
import type { ExcalidrawDocumentIdentity } from './identity'

const identity: ExcalidrawDocumentIdentity = {
  path: '/drawings/design.excalidraw',
  profile: 'default',
  runtime: 'local'
}
const source = JSON.stringify({
  type: 'excalidraw',
  version: 2,
  elements: [{ id: 'source' }],
  appState: {},
  files: {},
  custom: 'kept'
})

const sourceRead = () => ({
  path: identity.path,
  text: source,
  byteSize: source.length,
  fingerprint: 'source-fingerprint'
})

beforeEach(() => {
  vi.useFakeTimers()
  readDesktopDrawingFileText.mockReset()
  selectDesktopPaths.mockReset()
  writeDesktopDrawingFileText.mockReset()
  isDesktopFsRemoteMode.mockReturnValue(false)
  isDesktopFsWriteConflict.mockReturnValue(false)
  writeDesktopDrawingFileText.mockResolvedValue({ fingerprint: 'saved-fingerprint', path: identity.path })
})

describe('Excalidraw documents', () => {
  it('uses the identity-scoped filesystem and does not rewrite an invalid document', async () => {
    readDesktopDrawingFileText.mockResolvedValue({
      path: identity.path,
      text: '{bad json}',
      byteSize: 9,
      fingerprint: 'invalid'
    })

    await expect(loadDrawing(identity)).rejects.toThrow('Invalid Excalidraw document')
    expect(writeDesktopDrawingFileText).not.toHaveBeenCalled()
  })

  it('requires the Excalidraw root contract while defaulting omitted optional envelopes', async () => {
    const validSkillEnvelope = JSON.stringify({ type: 'excalidraw', version: 2, elements: [], skill: 'created' })
    readDesktopDrawingFileText.mockResolvedValue({
      path: identity.path,
      text: validSkillEnvelope,
      byteSize: validSkillEnvelope.length,
      fingerprint: 'skill-fingerprint'
    })

    await expect(loadDrawing(identity)).resolves.toMatchObject({
      appState: {},
      elements: [],
      envelope: { skill: 'created', type: 'excalidraw', version: 2 },
      files: {},
      fingerprint: 'skill-fingerprint'
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
      readDesktopDrawingFileText.mockResolvedValue({
        path: identity.path,
        text,
        byteSize: text.length,
        fingerprint: 'invalid'
      })
      await expect(loadDrawing(identity)).rejects.toThrow('Invalid Excalidraw document')
    }
  })
  it('discards serialized collaborators before handing app state to the editor', async () => {
    const text = JSON.stringify({
      type: 'excalidraw',
      version: 2,
      elements: [],
      appState: { collaborators: {} }
    })
    readDesktopDrawingFileText.mockResolvedValue({
      path: identity.path,
      text,
      byteSize: text.length,
      fingerprint: 'collaborators-fingerprint'
    })

    const drawing = await loadDrawing(identity)

    expect(drawing.appState).not.toHaveProperty('collaborators')
  })

  it('preserves the full document envelope and unknown keys when saving', async () => {
    readDesktopDrawingFileText.mockResolvedValue(sourceRead())
    const controller = createDrawingController(await loadDrawing(identity))

    controller.onSceneChange([{ id: 'updated', unknownElementKey: 'kept' }], { theme: 'dark' })
    await vi.runAllTimersAsync()

    expect(JSON.parse(writeDesktopDrawingFileText.mock.calls[0][1])).toMatchObject({
      custom: 'kept',
      elements: [{ id: 'updated', unknownElementKey: 'kept' }],
      appState: { theme: 'dark' },
      files: {}
    })
    expect(writeDesktopDrawingFileText).toHaveBeenCalledWith(identity, expect.any(String), 'source-fingerprint')
  })

  it('persists files received from the editor scene change', async () => {
    readDesktopDrawingFileText.mockResolvedValue(sourceRead())
    const controller = createDrawingController(await loadDrawing(identity))

    controller.onSceneChange([{ id: 'updated' }], {}, { image: { dataURL: 'data:image/png;base64,AA==' } })
    await vi.runAllTimersAsync()

    expect(JSON.parse(writeDesktopDrawingFileText.mock.calls[0][1]).files).toEqual({
      image: { dataURL: 'data:image/png;base64,AA==' }
    })
  })

  it('serializes saves and writes the newest pending scene', async () => {
    readDesktopDrawingFileText.mockResolvedValue(sourceRead())
    const controller = createDrawingController(await loadDrawing(identity))
    let resolveFirstWrite!: (value: { fingerprint: string; path: string }) => void
    const firstWrite = new Promise<{ fingerprint: string; path: string }>(resolve => {
      resolveFirstWrite = resolve
    })

    writeDesktopDrawingFileText.mockImplementationOnce(() => firstWrite)

    controller.onSceneChange([{ id: 'first' }], {})
    await vi.runAllTimersAsync()
    controller.onSceneChange([{ id: 'second' }], {})
    resolveFirstWrite!({ fingerprint: 'first-fingerprint', path: identity.path })
    await vi.runAllTimersAsync()

    expect(writeDesktopDrawingFileText).toHaveBeenLastCalledWith(
      identity,
      expect.stringContaining('second'),
      'first-fingerprint'
    )
  })

  it('does not overwrite a newer agent mutation when the authoritative compare-and-swap rejects its baseline', async () => {
    readDesktopDrawingFileText.mockResolvedValue(sourceRead())
    const controller = createDrawingController(await loadDrawing(identity))
    const conflict = new Error('409: document changed since baseline')
    writeDesktopDrawingFileText.mockRejectedValueOnce(conflict)
    isDesktopFsWriteConflict.mockReturnValueOnce(true)

    controller.onSceneChange([{ id: 'pane' }], {})
    await vi.runAllTimersAsync()

    expect(readDesktopDrawingFileText).toHaveBeenCalledTimes(1)
    expect(writeDesktopDrawingFileText).toHaveBeenCalledWith(
      identity,
      expect.stringContaining('pane'),
      'source-fingerprint'
    )
    expect(controller.getState().status).toBe('conflict')
  })

  it('conflicts on an external write and can explicitly keep the pane version from a fresh baseline', async () => {
    const disk = JSON.stringify({ ...JSON.parse(source), elements: [{ id: 'disk' }] })
    readDesktopDrawingFileText
      .mockResolvedValueOnce(sourceRead())
      .mockResolvedValueOnce({
        path: identity.path,
        text: disk,
        byteSize: disk.length,
        fingerprint: 'disk-fingerprint'
      })
      .mockResolvedValueOnce({
        path: identity.path,
        text: disk,
        byteSize: disk.length,
        fingerprint: 'disk-fingerprint'
      })
    const controller = createDrawingController(await loadDrawing(identity))
    writeDesktopDrawingFileText.mockRejectedValueOnce(new Error('409: document changed since baseline'))
    isDesktopFsWriteConflict.mockReturnValueOnce(true)

    controller.onSceneChange([{ id: 'pane' }], {})
    await vi.runAllTimersAsync()
    expect(controller.getState().status).toBe('conflict')

    await controller.keepPaneVersion()
    expect(writeDesktopDrawingFileText).toHaveBeenLastCalledWith(
      identity,
      expect.stringContaining('pane'),
      'disk-fingerprint'
    )

    await controller.reload()
    expect(controller.getState().elements).toEqual([{ id: 'disk' }])
  })

  it('retains edits after errors and exposes retry and Save As through the identity-scoped filesystem', async () => {
    readDesktopDrawingFileText.mockResolvedValue(sourceRead())
    writeDesktopDrawingFileText.mockRejectedValueOnce(new Error('offline'))
    const controller = createDrawingController(await loadDrawing(identity))

    controller.onSceneChange([{ id: 'pane' }], {})
    await vi.runAllTimersAsync()
    expect(controller.getState()).toMatchObject({ status: 'error', elements: [{ id: 'pane' }] })

    await controller.retry()
    expect(writeDesktopDrawingFileText).toHaveBeenLastCalledWith(
      identity,
      expect.stringContaining('pane'),
      'source-fingerprint'
    )

    selectDesktopPaths.mockResolvedValue(['/drawings/copy.excalidraw'])
    await expect(controller.saveAs()).resolves.toEqual({ ...identity, path: '/drawings/copy.excalidraw' })
    expect(selectDesktopPaths).toHaveBeenCalledWith(expect.objectContaining({ multiple: false }))
    expect(writeDesktopDrawingFileText).toHaveBeenLastCalledWith(
      { ...identity, path: '/drawings/copy.excalidraw' },
      expect.stringContaining('pane'),
      undefined
    )
    expect(controller.getState().identity).toEqual(identity)
  })

  it('reloads clean external replacements and conflicts dirty changes without writing', async () => {
    const replacement = JSON.stringify({ ...JSON.parse(source), elements: [{ id: 'external' }] })
    readDesktopDrawingFileText
      .mockResolvedValueOnce(sourceRead())
      .mockResolvedValueOnce({
        path: identity.path,
        text: replacement,
        byteSize: replacement.length,
        fingerprint: 'replacement-fingerprint'
      })
    const controller = createDrawingController(await loadDrawing(identity))
    let externalNotifications = 0
    controller.subscribe(origin => {
      if (origin === 'external') {
        externalNotifications += 1
      }
    })

    await controller.reconcileExternalChange('external-fingerprint')
    expect(controller.getState()).toMatchObject({
      elements: [{ id: 'external' }],
      fingerprint: 'replacement-fingerprint',
      status: 'ready'
    })
    expect(externalNotifications).toBe(1)

    controller.onSceneChange([{ id: 'pane' }], {})
    await controller.reconcileExternalChange('newer-fingerprint')
    expect(controller.getState().status).toBe('conflict')
    expect(writeDesktopDrawingFileText).not.toHaveBeenCalled()
  })

  it('flushes pending edits through the close lifecycle and refuses a conflicted pane', async () => {
    readDesktopDrawingFileText.mockResolvedValue(sourceRead())
    const controller = createDrawingController(await loadDrawing(identity))
    let resolveWrite!: (value: { fingerprint: string; path: string }) => void
    const write = new Promise<{ fingerprint: string; path: string }>(resolve => {
      resolveWrite = resolve
    })

    writeDesktopDrawingFileText.mockReturnValueOnce(write)
    controller.onSceneChange([{ id: 'pane' }], {})
    const closing = controller.waitForSave()
    expect(controller.canCloseCleanly()).toBe(false)
    resolveWrite!({ fingerprint: 'pane-fingerprint', path: identity.path })
    await closing
    expect(controller.canCloseCleanly()).toBe(true)

    controller.onSceneChange([{ id: 'pane' }], {})
    await controller.reconcileExternalChange('conflicting')
    expect(controller.canCloseCleanly()).toBe(false)
  })

  it('makes disposal await the pending save instead of cancelling its debounce', async () => {
    readDesktopDrawingFileText.mockResolvedValue(sourceRead())
    const controller = createDrawingController(await loadDrawing(identity))

    controller.onSceneChange([{ id: 'latest' }], {})
    const disposed = controller.dispose()
    expect(controller.canCloseCleanly()).toBe(false)
    await disposed

    expect(writeDesktopDrawingFileText).toHaveBeenCalledWith(
      identity,
      expect.stringContaining('latest'),
      'source-fingerprint'
    )
    expect(controller.canCloseCleanly()).toBe(true)
  })
})
