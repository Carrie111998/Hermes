import { describe, expect, it } from 'vitest'

import { TRANSLATIONS } from './catalog'
import type { Locale } from './types'

const LOCALES = Object.keys(TRANSLATIONS) as Locale[]

// The multi-pet copy added in PR4. Every locale must define the whole section —
// the type system enforces presence at compile time; this pins the runtime shape
// (string vs. parameterized function arity) so a locale can't ship a stub that
// type-checks but renders "undefined" or throws at call time (test 50).
const STRING_KEYS = [
  'modeTitle',
  'modeDesc',
  'followActive',
  'pinned',
  'rosterTitle',
  'rosterDesc',
  'connectionOnline',
  'connectionOffline',
  'connectionReauth',
  'connectionConnecting',
  'unavailable',
  'unavailableDesc',
  'removeAction',
  'enableAction'
] as const

describe('multi-pet i18n parity (test 50)', () => {
  it.each(LOCALES)('locale "%s" defines every multiPet string key', locale => {
    const multiPet = TRANSLATIONS[locale].settings.appearance.pet.multiPet

    for (const key of STRING_KEYS) {
      expect(typeof multiPet[key], `${locale}.multiPet.${key}`).toBe('string')
      expect((multiPet[key] as string).length, `${locale}.multiPet.${key} non-empty`).toBeGreaterThan(0)
    }
  })

  it.each(LOCALES)('locale "%s" parameterized multiPet keys return a string', locale => {
    const multiPet = TRANSLATIONS[locale].settings.appearance.pet.multiPet

    expect(typeof multiPet.profilePet('nova')).toBe('string')
    expect(typeof multiPet.softCapWarning(4)).toBe('string')
    expect(typeof multiPet.hardCapNotice(8)).toBe('string')
    expect(typeof multiPet.noActiveSession('nova')).toBe('string')
    expect(typeof multiPet.cannotQueueNoDurable('nova')).toBe('string')
  })

  it('interpolates the profile name into the inline notice copy', () => {
    // The English source of truth embeds the profile; guards against a locale
    // accidentally dropping the interpolation argument.
    expect(TRANSLATIONS.en.settings.appearance.pet.multiPet.noActiveSession('apollo')).toContain('apollo')
  })
})
