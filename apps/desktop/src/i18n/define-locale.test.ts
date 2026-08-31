import { describe, expect, it } from 'vitest'

import { defineLocale } from './define-locale'
import { en } from './en'


describe('defineLocale', () => {
  it('does not share untouched nested trees with English', () => {
    const japanese = defineLocale({ settings: { appearance: { title: '外観' } } })

    japanese.settings.nav.providers = 'mutated'

    expect(en.settings.nav.providers).toBe('Providers')
  })
})
