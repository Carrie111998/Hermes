import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { Intro } from './intro'

afterEach(cleanup)

beforeEach(() => {
  vi.stubGlobal(
    'matchMedia',
    vi.fn().mockImplementation(query => ({
      matches: false,
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn()
    }))
  )
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => undefined)
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('Intro portal figure', () => {
  it('renders the looping muted orb before the Hermes wordmark', () => {
    const { container } = render(<Intro personality="default" seed={1} />)
    const intro = container.querySelector('[data-slot="aui_intro"]')
    const video = container.querySelector<HTMLVideoElement>('[data-slot="homepage-orb-video"]')
    const wordmark = container.querySelector('[aria-label="HERMES AGENT"]')
    const subtitle = container.querySelector('[data-slot="homepage-brand-subtitle"]')

    expect(video).toBeTruthy()
    expect(video?.getAttribute('src')).toBe('./portal-figure-orb.webm')
    expect(video?.autoplay).toBe(true)
    expect(video?.loop).toBe(true)
    expect(video?.muted).toBe(true)
    expect(video?.playsInline).toBe(true)
    expect(subtitle?.textContent).toBe('AGK {OS}')
    expect(
      intro && video && wordmark ? video.compareDocumentPosition(wordmark) & Node.DOCUMENT_POSITION_FOLLOWING : 0
    ).toBeTruthy()
  })

  it('renders a static lazy-loaded frame when reduced motion is requested', () => {
    vi.stubGlobal(
      'matchMedia',
      vi.fn().mockImplementation(query => ({
        matches: query === '(prefers-reduced-motion: reduce)',
        media: query,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn()
      }))
    )

    const { container } = render(<Intro personality="default" seed={2} />)
    const video = container.querySelector<HTMLVideoElement>('[data-slot="homepage-orb-video"]')

    expect(video?.autoplay).toBe(false)
    expect(video?.loop).toBe(false)
    expect(video?.preload).toBe('metadata')
  })

  it('pauses when mounted hidden and resumes when visible', () => {
    let visibility: DocumentVisibilityState = 'hidden'
    vi.spyOn(globalThis.document, 'visibilityState', 'get').mockImplementation(() => visibility)
    const play = vi.mocked(HTMLMediaElement.prototype.play)
    const pause = vi.mocked(HTMLMediaElement.prototype.pause)
    play.mockClear()
    pause.mockClear()

    render(<Intro personality="default" seed={4} />)
    expect(pause).toHaveBeenCalledOnce()

    act(() => {
      visibility = 'visible'
      globalThis.document.dispatchEvent(new Event('visibilitychange'))
    })
    expect(play).toHaveBeenCalledOnce()
  })

  it('pauses and rewinds when reduced motion is enabled while playing', () => {
    let matches = false
    const listeners = new Set<() => void>()
    vi.stubGlobal(
      'matchMedia',
      vi.fn().mockImplementation(query => ({
        get matches() {
          return matches
        },
        media: query,
        addEventListener: (_event: string, listener: () => void) => listeners.add(listener),
        removeEventListener: (_event: string, listener: () => void) => listeners.delete(listener)
      }))
    )
    const pause = vi.mocked(HTMLMediaElement.prototype.pause)
    pause.mockClear()

    const { container } = render(<Intro personality="default" seed={3} />)
    const video = container.querySelector<HTMLVideoElement>('[data-slot="homepage-orb-video"]')
    expect(video).toBeTruthy()

    if (!video) {
      return
    }

    video.currentTime = 1.5

    act(() => {
      matches = true
      listeners.forEach(listener => listener())
    })

    expect(pause).toHaveBeenCalledOnce()
    expect(video.currentTime).toBe(0)
  })
})
