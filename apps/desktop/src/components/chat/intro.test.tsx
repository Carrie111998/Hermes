import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { Intro } from './intro'

afterEach(cleanup)

describe('Intro portal figure', () => {
  it('renders the looping muted orb before the Hermes wordmark', () => {
    const { container } = render(<Intro personality="default" seed={1} />)
    const intro = container.querySelector('[data-slot="aui_intro"]')
    const video = container.querySelector<HTMLVideoElement>('[data-slot="homepage-orb-video"]')
    const wordmark = container.querySelector('[aria-label="HERMES AGENT"]')
    const subtitle = container.querySelector('[data-slot="homepage-brand-subtitle"]')

    expect(video).toBeTruthy()
    expect(video?.getAttribute('src')).toBe('/portal-figure-orb.webm')
    expect(video?.autoplay).toBe(true)
    expect(video?.loop).toBe(true)
    expect(video?.muted).toBe(true)
    expect(video?.playsInline).toBe(true)
    expect(subtitle?.textContent).toBe('AGK {OS}')
    expect(intro && video && wordmark ? video.compareDocumentPosition(wordmark) & Node.DOCUMENT_POSITION_FOLLOWING : 0).toBeTruthy()
  })
})
