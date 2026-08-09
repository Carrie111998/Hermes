import { render } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { FrameEmbed } from './providers/types'

vi.mock('./use-is-dark', () => ({ useIsDark: () => false }))

import { EXTERNAL_FRAME_SANDBOX } from './embed-security'
import FrameEmbedRenderer from './frame-embed'
import YouTubeEmbedRenderer from './youtube-embed'

const frame: FrameEmbed = {
  aspectRatio: 16 / 9,
  embedUrl: 'https://player.vimeo.com/video/123',
  id: 'vimeo:123',
  label: 'Vimeo',
  maxWidth: 640,
  provider: 'vimeo',
  renderer: 'frame',
  sourceUrl: 'https://vimeo.com/123'
}

describe('external provider frames', () => {
  it('sandboxes generic provider frames without popup or top-navigation escapes', () => {
    const { container } = render(<FrameEmbedRenderer descriptor={frame} />)
    const iframe = container.querySelector('iframe')

    expect(iframe?.getAttribute('sandbox')).toBe(EXTERNAL_FRAME_SANDBOX)
    expect(EXTERNAL_FRAME_SANDBOX).not.toContain('allow-popups')
    expect(EXTERNAL_FRAME_SANDBOX).not.toContain('allow-top-navigation')
  })

  it('applies the same popup boundary to the dedicated YouTube renderer', () => {
    const { container } = render(<YouTubeEmbedRenderer descriptor={{ ...frame, provider: 'youtube' }} />)
    const iframe = container.querySelector('iframe')

    expect(iframe?.getAttribute('sandbox')).toBe(EXTERNAL_FRAME_SANDBOX)
  })

  it.each([
    ['Google Maps', 'googlemaps'],
    ['OpenStreetMap', 'openstreetmap'],
    ['Instagram', 'instagram'],
    ['Pinterest', 'pinterest'],
    ['TikTok', 'tiktok'],
    ['Vimeo', 'vimeo']
  ] as const)('keeps the %s frame on the shared sandbox policy', (_label, provider) => {
    const { container } = render(<FrameEmbedRenderer descriptor={{ ...frame, provider }} />)
    const iframe = container.querySelector('iframe')

    expect(iframe?.getAttribute('sandbox')).toBe(EXTERNAL_FRAME_SANDBOX)
  })

  it('keeps long Instagram posts accessible in an internal scrolling frame', () => {
    const { container } = render(
      <FrameEmbedRenderer
        descriptor={{
          ...frame,
          aspectRatio: undefined,
          embedUrl: 'https://www.instagram.com/p/CabcDEF123/embed',
          height: 450,
          provider: 'instagram'
        }}
      />
    )

    const iframe = container.querySelector('iframe')

    expect(iframe?.getAttribute('scrolling')).toBe('yes')
    expect(iframe?.style.height).toBe('450px')
    expect(iframe?.getAttribute('sandbox')).toBe(EXTERNAL_FRAME_SANDBOX)
  })
})
