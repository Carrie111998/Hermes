import { beforeEach, describe, expect, it, vi } from 'vitest'

const { requestComposerSubmit } = vi.hoisted(() => ({
  requestComposerSubmit: vi.fn()
}))

vi.mock('@/app/chat/composer/focus', () => ({ requestComposerSubmit }))

import { requestPrimarySessionCompression } from './context-compression-action'

describe('requestPrimarySessionCompression', () => {
  beforeEach(() => {
    requestComposerSubmit.mockReset()
  })

  it('reuses the primary composer /compress action', () => {
    requestPrimarySessionCompression()

    expect(requestComposerSubmit).toHaveBeenCalledOnce()
    expect(requestComposerSubmit).toHaveBeenCalledWith('/compress', { preserveDraft: true, target: 'main' })
  })
})
