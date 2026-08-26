import { describe, expect, it } from 'vitest'

import { canEmbedMobilePreviewUrl, shouldOpenMobilePreview } from './mobile-preview-policy'

const urlTab = (id: string, url: string) => ({
  id,
  target: { kind: 'url' as const, label: 'Browser', source: url, url },
})

describe('mobile preview policy', () => {
  it('opens the full-screen preview for a new or updated target, but not merely because a tab was closed', () => {
    const original = [urlTab('url:one', 'https://example.com')]

    expect(shouldOpenMobilePreview([], original)).toBe(true)
    expect(shouldOpenMobilePreview(original, [urlTab('url:one', 'https://example.com')])).toBe(true)
    expect(shouldOpenMobilePreview(original, [urlTab('url:one', 'https://example.com/next')])).toBe(true)
    expect(shouldOpenMobilePreview([...original, urlTab('url:two', 'https://example.org')], original)).toBe(false)
    expect(shouldOpenMobilePreview(original, [])).toBe(false)
  })

  it('embeds only HTTPS pages inside the mobile app', () => {
    expect(canEmbedMobilePreviewUrl('https://preview.example.test/path')).toBe(true)
    expect(canEmbedMobilePreviewUrl('http://preview.example.test/path')).toBe(false)
    expect(canEmbedMobilePreviewUrl('about:blank')).toBe(false)
    expect(canEmbedMobilePreviewUrl('javascript:alert(1)')).toBe(false)
  })
})
