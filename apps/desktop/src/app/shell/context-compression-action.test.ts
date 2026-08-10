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

  it('preserves the two most recent turns by default', () => {
    requestPrimarySessionCompression()

    expect(requestComposerSubmit).toHaveBeenCalledOnce()
    expect(requestComposerSubmit).toHaveBeenCalledWith('/compress here 2', {
      preserveDraft: true,
      target: 'main'
    })
  })

  it('preserves an explicitly selected recent-turn boundary', () => {
    requestPrimarySessionCompression(4)

    expect(requestComposerSubmit).toHaveBeenCalledWith('/compress here 4', {
      preserveDraft: true,
      target: 'main'
    })
  })

  it('normalizes a malformed selected boundary instead of emitting an invalid command', () => {
    requestPrimarySessionCompression(Number.NaN)

    expect(requestComposerSubmit).toHaveBeenCalledWith('/compress here 2', {
      preserveDraft: true,
      target: 'main'
    })
  })

  it('retains the existing full-compression option', () => {
    requestPrimarySessionCompression(null)

    expect(requestComposerSubmit).toHaveBeenCalledWith('/compress', {
      preserveDraft: true,
      target: 'main'
    })
  })
})
