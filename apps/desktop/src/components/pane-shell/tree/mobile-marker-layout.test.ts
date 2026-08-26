import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const originalMatchMedia = window.matchMedia

describe('mobile renderer layout bootstrap', () => {
  beforeEach(() => {
    document.documentElement.removeAttribute('data-hermes-mobile')
    vi.resetModules()
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      value: vi.fn(() => ({ addEventListener: vi.fn(), matches: false, removeEventListener: vi.fn() })),
    })
  })

  afterEach(() => {
    document.documentElement.removeAttribute('data-hermes-mobile')
    Object.defineProperty(window, 'matchMedia', { configurable: true, value: originalMatchMedia })
  })

  it('keeps an unfolded native mobile renderer in narrow drawer mode before the pane store initializes', async () => {
    const { markNativeMobileRenderer } = await import('../../../../../mobile/src/mobile/runtime-marker')
    markNativeMobileRenderer()

    const { $narrowViewport } = await import('./store')

    expect(window.matchMedia).toHaveBeenCalled()
    expect($narrowViewport.get()).toBe(true)
  })
})
