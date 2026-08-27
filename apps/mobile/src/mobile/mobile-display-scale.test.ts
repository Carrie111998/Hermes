// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { preferences } = vi.hoisted(() => ({ preferences: { get: vi.fn(), set: vi.fn() } }))
vi.mock('@capacitor/preferences', () => ({ Preferences: preferences }))

import { $mobileDisplayScale, adjustMobileDisplayScale, loadMobileDisplayScale, resetMobileDisplayScale, setMobileDisplayScale } from './mobile-display-scale'

beforeEach(() => {
  document.documentElement.removeAttribute('style')
  preferences.get.mockReset()
  preferences.set.mockReset()
  resetMobileDisplayScale()
})

describe('mobile display scale', () => {
  it('clamps presentation settings without using CSS zoom or changing fixed safe-area controls', () => {
    setMobileDisplayScale({ overall: 9, text: 0 })
    expect($mobileDisplayScale.get()).toEqual({ overall: 1.2, text: 0.85 })
    expect(document.documentElement.style.getPropertyValue('zoom')).toBe('')
    expect(document.documentElement.style.getPropertyValue('--dt-base-size')).toBe('1.2rem')
    expect(document.documentElement.style.getPropertyValue('--mobile-conversation-text-scale')).toBe('0.85')
  })

  it('persists and restores independent UI and reading scales', async () => {
    adjustMobileDisplayScale('overall', 0.05)
    adjustMobileDisplayScale('text', -0.1)
    expect($mobileDisplayScale.get()).toEqual({ overall: 1.05, text: 0.9 })
    expect(preferences.set).toHaveBeenCalled()

    preferences.get.mockResolvedValue({ value: JSON.stringify({ overall: 0.9, text: 1.1 }) })
    await loadMobileDisplayScale()
    expect($mobileDisplayScale.get()).toEqual({ overall: 0.9, text: 1.1 })
  })
})
