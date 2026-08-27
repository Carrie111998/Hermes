import { describe, expect, it } from 'vitest'

import { $previewTabs } from '@/store/preview'

import { hasLivePreviewForSession, registerPreviewPageReader } from './preview-reader'

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
})
