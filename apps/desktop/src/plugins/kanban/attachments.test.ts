import { describe, expect, it, vi } from 'vitest'

// The SDK's import chain touches the app API client; the existing kanban tests
// stub it the same way (see model-override.test.tsx).
vi.mock('@/hermes', () => ({
  getGlobalModelOptions: vi.fn(),
  setApiRequestProfile: vi.fn()
}))

import { attachmentFileUrl, bindApi, canOpenAttachments, openAttachmentFile } from './api'

describe('attachmentFileUrl', () => {
  it('wraps a plain absolute path in file://', () => {
    expect(attachmentFileUrl('/Users/derin/.hermes/kanban/attachments/t_1/report.md')).toBe(
      'file:///Users/derin/.hermes/kanban/attachments/t_1/report.md'
    )
  })

  it('keeps an already-file:// path untouched', () => {
    expect(attachmentFileUrl('file:///tmp/a.md')).toBe('file:///tmp/a.md')
  })

  it('gives Windows drive letters the leading slash they need', () => {
    expect(
      attachmentFileUrl('C:\\Users\\derin\\.hermes\\kanban\\attachments\\t_1\\report.md')
    ).toBe('file:///C:/Users/derin/.hermes/kanban/attachments/t_1/report.md')
  })

  it('percent-encodes #, spaces, and non-ASCII characters', () => {
    expect(attachmentFileUrl('/Users/derin/a#b c/ü.md')).toBe(
      'file:///Users/derin/a%23b%20c/%C3%BC.md'
    )
  })
})

describe('attachment OS door', () => {
  const fakeOs = { openExternal: vi.fn(async () => true) } as unknown as Parameters<typeof bindApi>[3]
  const fakeStorage = {
    get: vi.fn(async () => null),
    set: vi.fn(async () => {}),
    remove: vi.fn(async () => {})
  } as unknown as Parameters<typeof bindApi>[1]
  const fakeSocket = vi.fn(() => vi.fn()) as unknown as Parameters<typeof bindApi>[2]

  it('is unavailable before bindApi and resolves false on open', async () => {
    expect(canOpenAttachments()).toBe(false)
    await expect(openAttachmentFile('/tmp/a.md')).resolves.toBe(false)
  })

  it('opens via the OS door once bound, with a file URL', async () => {
    const unbind = bindApi(vi.fn(), fakeStorage, fakeSocket, fakeOs)
    expect(canOpenAttachments()).toBe(true)
    await expect(openAttachmentFile('C:\\tmp\\a#b.md')).resolves.toBe(true)
    expect(fakeOs.openExternal).toHaveBeenCalledWith('file:///C:/tmp/a%23b.md')
    unbind()
    expect(canOpenAttachments()).toBe(false)
  })
})
