// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest'

vi.hoisted(() => {
  Object.defineProperty(globalThis.navigator, 'platform', { configurable: true, value: 'Win32' })
  ;(globalThis.window as Window).hermesDesktop = {
    glassSupported: false,
    translucencySupported: true
  } as Window['hermesDesktop']
})

import {
  $translucency,
  defaultTranslucencyValues,
  GLASS_SUPPORTED,
  TRANSLUCENCY_SUPPORTED
} from './translucency'

describe('Windows 10 translucency defaults', () => {
  it('uses Windows defaults even when glass is unsupported', () => {
    expect(GLASS_SUPPORTED).toBe(false)
    expect(TRANSLUCENCY_SUPPORTED).toBe(true)
    expect($translucency.get()).toEqual({
      ...defaultTranslucencyValues('dark', true),
      mode: 'clear'
    })
  })
})
