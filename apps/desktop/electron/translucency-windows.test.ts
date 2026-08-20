import { describe, expect, it } from 'vitest'

import {
  DEFAULT_GLASS_MATERIAL,
  DEFAULT_GLASS_SCOPE,
  type TranslucencyState,
  windowBackingOptionsForPlatform
} from './translucency'

const state = (intensity: number, mode: 'clear' | 'glass'): TranslucencyState => ({
  intensity,
  fade: 0,
  mode,
  material: DEFAULT_GLASS_MATERIAL,
  scope: DEFAULT_GLASS_SCOPE
})

describe('Windows chat window backing', () => {
  it('keeps the BrowserWindow opaque while glass is off', () => {
    expect(windowBackingOptionsForPlatform(state(0, 'glass'), '#111111', 'win32')).toEqual({
      backgroundColor: '#111111',
      backgroundMaterial: undefined,
      transparent: false
    })

    expect(windowBackingOptionsForPlatform(state(60, 'clear'), '#111111', 'win32')).toEqual({
      backgroundColor: '#111111',
      backgroundMaterial: undefined,
      transparent: false
    })
  })

  it('leaves glass-active windows available for the native material', () => {
    expect(windowBackingOptionsForPlatform(state(60, 'glass'), '#111111', 'win32')).toEqual({})
  })
})
