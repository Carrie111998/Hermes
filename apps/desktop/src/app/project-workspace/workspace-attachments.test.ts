import { beforeEach, describe, expect, it, vi } from 'vitest'

import { pickWorkspaceAttachments } from './workspace-attachments'

vi.mock('@/lib/desktop-fs', () => ({
  readDesktopFileDataUrl: vi.fn(),
  selectDesktopPaths: vi.fn()
}))

const desktopFs = await import('@/lib/desktop-fs')
const readDesktopFileDataUrl = vi.mocked(desktopFs.readDesktopFileDataUrl)
const selectDesktopPaths = vi.mocked(desktopFs.selectDesktopPaths)

beforeEach(() => {
  selectDesktopPaths.mockReset()
  readDesktopFileDataUrl.mockReset()
})

describe('pickWorkspaceAttachments', () => {
  it('creates repository-relative file references', async () => {
    selectDesktopPaths.mockResolvedValue(['/repo/docs/brief.pdf'])

    const attachments = await pickWorkspaceAttachments({ cwd: '/repo', kind: 'file' })

    expect(attachments).toEqual([
      expect.objectContaining({
        detail: 'docs/brief.pdf',
        kind: 'file',
        label: 'brief.pdf',
        path: '/repo/docs/brief.pdf',
        refText: '@file:docs/brief.pdf'
      })
    ])
  })

  it('loads image previews without writing the image into the repository', async () => {
    selectDesktopPaths.mockResolvedValue(['/outside/reference.png'])
    readDesktopFileDataUrl.mockResolvedValue('data:image/png;base64,AAAA')

    const attachments = await pickWorkspaceAttachments({ cwd: '/repo', kind: 'image' })

    expect(attachments).toEqual([
      expect.objectContaining({
        kind: 'image',
        label: 'reference.png',
        path: '/outside/reference.png',
        previewUrl: 'data:image/png;base64,AAAA'
      })
    ])
    expect(readDesktopFileDataUrl).toHaveBeenCalledWith('/outside/reference.png')
  })

  it('returns no artifacts when the user cancels', async () => {
    selectDesktopPaths.mockResolvedValue([])

    await expect(pickWorkspaceAttachments({ cwd: '/repo', kind: 'file' })).resolves.toEqual([])
  })
})
