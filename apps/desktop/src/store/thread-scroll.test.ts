import { afterEach, describe, expect, it, vi } from 'vitest'

import { jumpThreadScroll } from './thread-scroll'

const originalMatchMedia = window.matchMedia

afterEach(() => {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: originalMatchMedia
  })
  vi.restoreAllMocks()
})

describe('jumpThreadScroll', () => {
  it('moves immediately without animation when reduced motion is requested', () => {
    const viewport = document.createElement('div')
    viewport.scrollTop = 800
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      value: vi.fn(() => ({ matches: true }))
    })
    const requestAnimationFrame = vi.spyOn(window, 'requestAnimationFrame')

    jumpThreadScroll(viewport, 300)

    expect(viewport.scrollTop).toBe(300)
    expect(requestAnimationFrame).not.toHaveBeenCalled()
  })

  it('cancels an earlier jump only within the same transcript viewport', () => {
    const firstViewport = document.createElement('div')
    const secondViewport = document.createElement('div')
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      value: vi.fn(() => ({ matches: false }))
    })

    const requestAnimationFrame = vi
      .spyOn(window, 'requestAnimationFrame')
      .mockReturnValueOnce(11)
      .mockReturnValueOnce(22)
      .mockReturnValueOnce(33)

    const cancelAnimationFrame = vi.spyOn(window, 'cancelAnimationFrame')

    jumpThreadScroll(firstViewport, 100)
    jumpThreadScroll(secondViewport, 200)

    expect(cancelAnimationFrame).not.toHaveBeenCalled()

    jumpThreadScroll(firstViewport, 300)

    expect(cancelAnimationFrame).toHaveBeenCalledOnce()
    expect(cancelAnimationFrame).toHaveBeenCalledWith(11)
    expect(requestAnimationFrame).toHaveBeenCalledTimes(3)
  })
})
