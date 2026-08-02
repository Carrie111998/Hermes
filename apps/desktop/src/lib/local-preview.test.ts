import { afterEach, describe, expect, it, vi } from 'vitest'

import { $connection } from '@/store/session'

import { localPreviewTarget, normalizeOrLocalPreviewTarget } from './local-preview'

afterEach(() => {
  $connection.set(null)
  vi.unstubAllGlobals()
})

describe('localPreviewTarget', () => {
  it('classifies PDF files as PDF previews', () => {
    expect(localPreviewTarget('/tmp/spec.pdf')).toMatchObject({
      path: '/tmp/spec.pdf',
      previewKind: 'pdf'
    })
  })

  it('keeps ordinary text files on the source-preview path', () => {
    expect(localPreviewTarget('/tmp/spec.md')).toMatchObject({
      language: 'markdown',
      previewKind: 'text'
    })
  })

  it('does not UTF-8-enrich remote PDFs before loading their bytes', async () => {
    const api = vi.fn()

    $connection.set({ mode: 'remote', profile: 'macmini' } as never)
    vi.stubGlobal('window', {
      ...window,
      hermesDesktop: {
        api,
        normalizePreviewTarget: vi.fn(async () => null)
      }
    })

    const target = await normalizeOrLocalPreviewTarget('/remote/spec.pdf')

    expect(target).toMatchObject({ path: '/remote/spec.pdf', previewKind: 'pdf' })
    expect(api).not.toHaveBeenCalled()
  })
})
