import { describe, expect, it } from 'vitest'

import { $previewTabs } from '@/store/preview'

import { getLivePreviewTabIdForSession, hasLivePreviewForSession, registerPreviewPageReader } from './preview-reader'

describe('session-scoped preview reader gate (#95459)', () => {
  const setupTabs = () => {
    $previewTabs.set([
      { id: 'url:tab-a', target: { kind: 'url', label: 'Browser', source: 'https://example.com', url: 'https://example.com' } },
    ])
  }

  it('rejects when a different session owns the live preview', () => {
    setupTabs()
    const unregister = registerPreviewPageReader('url:tab-a', async () => ({ text: '', title: '', url: '' }), 'session-A')

    // Session B asks: should be rejected (session-A owns the preview)
    expect(hasLivePreviewForSession('session-B')).toBe(false)

    // Session A asks: should be accepted (it owns the preview)
    expect(hasLivePreviewForSession('session-A')).toBe(true)

    unregister()
  })

  it('accepts the owning session after restart re-bind', () => {
    setupTabs()
    const unregister = registerPreviewPageReader('url:tab-a', async () => ({ text: '', title: '', url: '' }), 'session-owner')

    expect(hasLivePreviewForSession('session-owner')).toBe(true)
    expect(hasLivePreviewForSession('session-other')).toBe(false)

    unregister()
  })

  it('rejects when reader is unregistered', () => {
    setupTabs()
    const unregister = registerPreviewPageReader('url:tab-a', async () => ({ text: '', title: '', url: '' }), 'session-A')

    unregister()
    expect(hasLivePreviewForSession('session-A')).toBe(false)
  })

  it('empty sessionId always rejects', () => {
    setupTabs()
    const unregister = registerPreviewPageReader('url:tab-a', async () => ({ text: '', title: '', url: '' }), 'session-A')

    expect(hasLivePreviewForSession('')).toBe(false)

    unregister()
  })

  it('returns the owned tab ID so admission and effect bind to one identity', () => {
    setupTabs()
    const unregister = registerPreviewPageReader('url:tab-a', async () => ({ text: '', title: '', url: '' }), 'session-A')

    // Owning session resolves the exact tab it owns...
    expect(getLivePreviewTabIdForSession('session-A')).toBe('url:tab-a')

    // ...while a non-owning session resolves nothing.
    expect(getLivePreviewTabIdForSession('session-B')).toBeNull()

    unregister()
  })

  it('owned tab ID is null after unregister', () => {
    setupTabs()
    const unregister = registerPreviewPageReader('url:tab-a', async () => ({ text: '', title: '', url: '' }), 'session-A')

    unregister()
    expect(getLivePreviewTabIdForSession('session-A')).toBeNull()
  })
})
