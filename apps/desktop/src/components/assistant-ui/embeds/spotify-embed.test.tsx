import { render } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { FrameEmbed } from './providers/types'

vi.mock('./use-is-dark', () => ({ useIsDark: () => false }))

import { EXTERNAL_FRAME_SANDBOX } from './embed-security'
import SpotifyEmbedRenderer from './spotify-embed'

const spotify: FrameEmbed = {
  embedUrl: 'https://open.spotify.com/embed/track/4cOdK2wGLETKBW3PvgPWqT',
  height: 152,
  id: 'spotify:track:4cOdK2wGLETKBW3PvgPWqT',
  label: 'Spotify',
  maxWidth: 480,
  provider: 'spotify',
  renderer: 'frame',
  sourceUrl: 'https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT'
}

describe('Spotify provider renderer', () => {
  it('uses the shared sandbox policy', () => {
    const { container } = render(<SpotifyEmbedRenderer descriptor={spotify} />)
    const iframe = container.querySelector('iframe')

    expect(iframe?.getAttribute('sandbox')).toBe(EXTERNAL_FRAME_SANDBOX)
    expect(EXTERNAL_FRAME_SANDBOX).not.toContain('allow-popups')
    expect(EXTERNAL_FRAME_SANDBOX).not.toContain('allow-top-navigation')
  })
})
