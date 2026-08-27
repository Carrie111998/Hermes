import { describe, expect, it } from 'vitest'

import { $previewTabs } from '@/store/preview'

import {
  getLivePreviewTabIdForSession,
  hasLivePreviewForSession,
  isLivePreviewTabOwnedBySession,
  registerPreviewPageReader
} from './preview-reader'

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

  it('resolves ownership from the exact active tab, not the first owned tab (#95459 review)', () => {
    // Deterministic witness from the review: session S owns live previews A
    // and B (registered in that order), and B is the active one. Authorization
    // must ask about B specifically — getLivePreviewTabIdForSession(S) would
    // return A (the first owned tab), which is the wrong identity to compare
    // against the active preview.
    $previewTabs.set([
      { id: 'url:tab-a', target: { kind: 'url', label: 'Browser', source: 'https://x', url: 'https://x' } },
      { id: 'url:tab-b', target: { kind: 'url', label: 'Browser', source: 'https://y', url: 'https://y' } }
    ])

    const unregisterA = registerPreviewPageReader('url:tab-a', async () => ({ text: '', title: '', url: '' }), 'session-S')
    const unregisterB = registerPreviewPageReader('url:tab-b', async () => ({ text: '', title: '', url: '' }), 'session-S')

    // The active tab B IS owned by S -> allowed.
    expect(isLivePreviewTabOwnedBySession('url:tab-b', 'session-S')).toBe(true)
    // A non-active tab owned by S also answers true for itself (the mutation
    // targets that tab), but the admission layer gates on the ACTIVE tab.
    expect(isLivePreviewTabOwnedBySession('url:tab-a', 'session-S')).toBe(true)
    // A different session does not own either.
    expect(isLivePreviewTabOwnedBySession('url:tab-b', 'session-other')).toBe(false)

    unregisterA()
    unregisterB()
  })
})
