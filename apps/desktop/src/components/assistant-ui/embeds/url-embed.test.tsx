import { act, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setEmbedMode } from '@/store/embed-consent'

import { EXTERNAL_FRAME_SANDBOX } from './embed-security'
import { detectEmbed } from './providers'
import { UrlEmbed } from './url-embed'

vi.mock('./use-is-dark', () => ({ useIsDark: () => false }))

describe('UrlEmbed provider routing', () => {
  beforeEach(() => act(() => setEmbedMode('always')))
  afterEach(() => act(() => setEmbedMode('ask')))

  it('routes Instagram through the sandboxed frame renderer without scripts', async () => {
    const descriptor = detectEmbed('https://www.instagram.com/p/CabcDEF123/')

    if (!descriptor || descriptor.renderer !== 'frame') {
      throw new Error('expected an Instagram frame descriptor')
    }

    const { container } = render(<UrlEmbed descriptor={descriptor} />)

    await waitFor(() => expect(container.querySelector('iframe')).not.toBeNull())

    expect(container.querySelector('iframe')?.getAttribute('sandbox')).toBe(EXTERNAL_FRAME_SANDBOX)
    expect(container.querySelectorAll('script')).toHaveLength(0)
  })
})
