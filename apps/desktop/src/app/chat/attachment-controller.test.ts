import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { ComposerAttachment } from '@/store/composer'

const mocks = vi.hoisted(() => ({
  extractDroppedFiles: vi.fn(),
  partitionDroppedFiles: vi.fn((candidates: Array<{ file?: File; path: string }>) => ({
    inAppRefs: candidates.filter(candidate => !candidate.file),
    osDrops: candidates.filter(candidate => candidate.file)
  })),
  selectDesktopPaths: vi.fn(),
  uploadComposerAttachment: vi.fn()
}))

vi.mock('@/app/chat/hooks/use-composer-actions', () => ({
  extractDroppedFiles: mocks.extractDroppedFiles,
  isImagePath: (path: string) => /\.(?:png|jpe?g|gif|webp)$/i.test(path),
  partitionDroppedFiles: mocks.partitionDroppedFiles
}))

vi.mock('@/lib/desktop-fs', () => ({
  selectDesktopPaths: mocks.selectDesktopPaths
}))

vi.mock('@/lib/chat-runtime', () => ({
  attachmentDisplayText: (attachment: ComposerAttachment) =>
    attachment.refText || (attachment.kind === 'image' && attachment.path ? `@image:${attachment.path}` : null),
  attachmentId: (kind: string, path: string) => `${kind}:${path}`,
  pathLabel: (path: string) => path.split(/[\\/]/).filter(Boolean).pop() || path
}))

vi.mock('@/app/session/hooks/use-prompt-actions', () => ({
  uploadComposerAttachment: mocks.uploadComposerAttachment
}))

import {
  type AttachmentControllerError,
  type AttachmentStageTarget,
  createAttachmentController
} from './attachment-controller'

const RAW_PATH = '/Users/alice/Private/report.pdf'

function target(overrides: Partial<AttachmentStageTarget> = {}): AttachmentStageTarget {
  return {
    remote: false,
    requestGateway: vi.fn(async () => ({} as never)),
    routeKey: 'local:default',
    sessionId: 'runtime-1',
    storedSessionId: 'stored-1',
    ...overrides
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })

  return { promise, reject, resolve }
}

function stagedFile(attachment: ComposerAttachment, sessionId: string, refText = '@file:.hermes/report.pdf') {
  return {
    ...attachment,
    attachedSessionId: sessionId,
    refText,
    uploadState: undefined
  } satisfies ComposerAttachment
}

describe('createAttachmentController', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.selectDesktopPaths.mockResolvedValue([])
    mocks.uploadComposerAttachment.mockImplementation(async (attachment: ComposerAttachment, options: { sessionId: string }) =>
      stagedFile(attachment, options.sessionId)
    )
  })

  it('picks through the Desktop picker and exposes only opaque, immutable metadata', async () => {
    mocks.selectDesktopPaths.mockResolvedValue([RAW_PATH])
    const controller = createAttachmentController({ contextKey: 'channel:alpha' })

    await expect(controller.pickFiles({ defaultPath: '/Users/alice', title: 'Attach to channel' })).resolves.toEqual({
      added: 1,
      rejected: 0
    })

    expect(mocks.selectDesktopPaths).toHaveBeenCalledWith({
      defaultPath: '/Users/alice',
      directories: false,
      multiple: true,
      title: 'Attach to channel'
    })

    const visible = controller.$attachments.get()
    const snapshot = controller.snapshot()

    expect(visible).toHaveLength(1)
    expect(visible[0]).toMatchObject({ kind: 'file', label: 'report.pdf', status: 'ready' })
    expect(visible[0]?.id).not.toContain(RAW_PATH)
    expect(JSON.stringify(visible)).not.toContain(RAW_PATH)
    expect(JSON.stringify(snapshot)).not.toContain(RAW_PATH)
    expect(JSON.stringify(snapshot)).not.toContain('data:')
    expect(Object.isFrozen(snapshot)).toBe(true)
    expect(Object.isFrozen(snapshot.attachments)).toBe(true)
    expect(Object.isFrozen(snapshot.attachments[0])).toBe(true)
  })

  it('delegates staging to the hardened core uploader and returns only its canonical ref', async () => {
    mocks.selectDesktopPaths.mockResolvedValue([RAW_PATH])
    const controller = createAttachmentController({ contextKey: 'channel:alpha' })
    await controller.pickFiles()
    const snapshot = controller.snapshot()
    const requestGateway = vi.fn(async () => ({} as never))

    const result = await controller.stage(
      snapshot,
      target({
        backendCwd: '/workspace',
        remote: true,
        requestGateway,
        routeKey: 'ssh-a:bot-a',
        sessionId: 'runtime-a',
        storedSessionId: 'stored-a',
        terminalBackend: 'ssh'
      })
    )

    expect(mocks.uploadComposerAttachment).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: 'file',
        label: 'report.pdf',
        occurrenceId: snapshot.attachments[0]?.id,
        path: RAW_PATH
      }),
      expect.objectContaining({
        backendCwd: '/workspace',
        remote: true,
        requestGateway,
        sessionId: 'runtime-a',
        storedSessionId: 'stored-a',
        terminalBackend: 'ssh'
      })
    )
    expect(result).toEqual({
      attachments: [
        {
          id: snapshot.attachments[0]?.id,
          kind: 'file',
          label: 'report.pdf',
          refText: '@file:.hermes/report.pdf'
        }
      ],
      sessionId: 'runtime-a'
    })
    expect(JSON.stringify(result)).not.toContain(RAW_PATH)
    expect(JSON.stringify(result)).not.toContain('data:')
  })

  it('single-flights concurrent staging for the same occurrence and target', async () => {
    mocks.selectDesktopPaths.mockResolvedValue([RAW_PATH])
    const gate = deferred<ComposerAttachment>()
    mocks.uploadComposerAttachment.mockReturnValue(gate.promise)
    const controller = createAttachmentController()
    await controller.pickFiles()
    const snapshot = controller.snapshot()
    const stageTarget = target()

    const first = controller.stage(snapshot, stageTarget)
    const second = controller.stage(snapshot, stageTarget)
    await vi.waitFor(() => expect(mocks.uploadComposerAttachment).toHaveBeenCalledTimes(1))
    expect(controller.$attachments.get()[0]?.status).toBe('staging')

    gate.resolve(stagedFile({ id: 'internal', kind: 'file', label: 'report.pdf' }, 'runtime-1'))

    await expect(Promise.all([first, second])).resolves.toEqual([
      expect.objectContaining({ sessionId: 'runtime-1' }),
      expect.objectContaining({ sessionId: 'runtime-1' })
    ])
    expect(mocks.uploadComposerAttachment).toHaveBeenCalledTimes(1)
    expect(controller.$attachments.get()[0]?.status).toBe('ready')
  })

  it('keys staged results by route and session while caching an exact repeat', async () => {
    mocks.selectDesktopPaths.mockResolvedValue([RAW_PATH])
    mocks.uploadComposerAttachment.mockImplementation(async (attachment: ComposerAttachment, options: { sessionId: string }) =>
      stagedFile(attachment, options.sessionId, `@file:.hermes/${options.sessionId}.pdf`)
    )
    const controller = createAttachmentController()
    await controller.pickFiles()
    const snapshot = controller.snapshot()

    const first = await controller.stage(snapshot, target({ routeKey: 'source-a:bot', sessionId: 'session-a' }))
    const second = await controller.stage(snapshot, target({ routeKey: 'source-b:bot', sessionId: 'session-b' }))
    const repeat = await controller.stage(snapshot, target({ routeKey: 'source-a:bot', sessionId: 'session-a' }))

    expect(mocks.uploadComposerAttachment).toHaveBeenCalledTimes(2)
    expect(first.attachments[0]?.refText).toBe('@file:.hermes/session-a.pdf')
    expect(second.attachments[0]?.refText).toBe('@file:.hermes/session-b.pdf')
    expect(repeat).toEqual(first)
  })

  it('ignores a late completion after remove and same-path re-add', async () => {
    mocks.selectDesktopPaths.mockResolvedValue([RAW_PATH])
    const gate = deferred<ComposerAttachment>()
    mocks.uploadComposerAttachment.mockReturnValueOnce(gate.promise)
    const controller = createAttachmentController()
    await controller.pickFiles()
    const snapshot = controller.snapshot()
    const staleId = snapshot.attachments[0]!.id

    const staging = controller.stage(snapshot, target())
    await vi.waitFor(() => expect(controller.$attachments.get()[0]?.status).toBe('staging'))
    expect(controller.remove(staleId)).toBe(true)
    await controller.pickFiles()
    const replacement = controller.$attachments.get()[0]!
    expect(replacement.id).not.toBe(staleId)

    gate.resolve(stagedFile({ id: 'old', kind: 'file', label: 'report.pdf' }, 'runtime-1'))

    await expect(staging).rejects.toMatchObject({ code: 'stale' })
    expect(controller.$attachments.get()).toEqual([{ ...replacement, status: 'ready' }])
  })

  it('ignores a late completion after the channel context changes', async () => {
    mocks.selectDesktopPaths.mockResolvedValue([RAW_PATH])
    const gate = deferred<ComposerAttachment>()
    mocks.uploadComposerAttachment.mockReturnValueOnce(gate.promise)
    const controller = createAttachmentController({ contextKey: 'channel:a' })
    await controller.pickFiles()

    const staging = controller.stage(controller.snapshot(), target())
    await vi.waitFor(() => expect(mocks.uploadComposerAttachment).toHaveBeenCalledTimes(1))
    controller.setContext('channel:b')
    gate.resolve(stagedFile({ id: 'old', kind: 'file', label: 'report.pdf' }, 'runtime-1'))

    await expect(staging).rejects.toMatchObject({ code: 'stale' })
    expect(controller.$attachments.get()[0]?.status).toBe('ready')
  })

  it('makes staging errors observable and retries only after an explicit second call', async () => {
    mocks.selectDesktopPaths.mockResolvedValue([RAW_PATH])
    mocks.uploadComposerAttachment.mockRejectedValueOnce(new Error('gateway unavailable'))
    const controller = createAttachmentController()
    await controller.pickFiles()
    const snapshot = controller.snapshot()

    await expect(controller.stage(snapshot, target())).rejects.toThrow('gateway unavailable')
    expect(controller.$attachments.get()[0]).toMatchObject({ error: 'Attachment staging failed.', status: 'error' })
    expect(mocks.uploadComposerAttachment).toHaveBeenCalledTimes(1)

    await Promise.resolve()
    expect(mocks.uploadComposerAttachment).toHaveBeenCalledTimes(1)

    mocks.uploadComposerAttachment.mockImplementationOnce(async (attachment: ComposerAttachment, options: { sessionId: string }) =>
      stagedFile(attachment, options.sessionId)
    )
    await expect(controller.stage(snapshot, target())).resolves.toMatchObject({ sessionId: 'runtime-1' })
    expect(mocks.uploadComposerAttachment).toHaveBeenCalledTimes(2)
    expect(controller.$attachments.get()[0]?.status).toBe('ready')
  })

  it('reuses the core drop partition and fails closed for path-only in-app refs', () => {
    const file = new File(['hello'], 'report.pdf', { type: 'application/pdf' })
    const transfer = {} as DataTransfer
    mocks.extractDroppedFiles.mockReturnValue([
      { path: '/gateway/project/README.md' },
      { file, path: RAW_PATH }
    ])
    const controller = createAttachmentController()

    expect(controller.addDropped(transfer)).toEqual({ added: 1, rejected: 1 })
    expect(mocks.extractDroppedFiles).toHaveBeenCalledWith(transfer)
    expect(mocks.partitionDroppedFiles).toHaveBeenCalled()
    expect(controller.$attachments.get()[0]).toMatchObject({ kind: 'file', label: 'report.pdf', status: 'ready' })
    expect(JSON.stringify(controller.$attachments.get())).not.toContain(RAW_PATH)
  })

  it('rejects incomplete route/session targets before any gateway side effect', async () => {
    mocks.selectDesktopPaths.mockResolvedValue([RAW_PATH])
    const controller = createAttachmentController()
    await controller.pickFiles()

    await expect(controller.stage(controller.snapshot(), target({ routeKey: '' }))).rejects.toEqual(
      expect.objectContaining<Partial<AttachmentControllerError>>({ code: 'invalid-target' })
    )
    expect(mocks.uploadComposerAttachment).not.toHaveBeenCalled()
  })
})
