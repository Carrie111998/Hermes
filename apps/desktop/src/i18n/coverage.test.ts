import { describe, expect, it } from 'vitest'

import { TRANSLATIONS } from './catalog'
import { authoredStrings } from './define-locale'
import { en } from './en'
import { LOCALE_OPTIONS } from './languages'
import type { Locale } from './types'

/**
 * Ceiling on the English strings each locale may leave untranslated.
 *
 * This exists because an untranslated string is otherwise invisible:
 * `defineLocale` merges every locale over `en`, so a key nobody translated
 * still renders — in English — and every field of `TranslationOverrides` is
 * optional, so `tsc` cannot flag it either. Nothing surfaces the gap until a
 * user reports it.
 *
 * The numbers only ever go down. Translating more of a locale is free; lowering
 * its ceiling afterwards keeps the ratchet tight. Shipping an English string
 * without translating it pushes a locale past its ceiling and fails here.
 */
const MAX_UNTRANSLATED: Record<Locale, number> = {
  en: 0,
  ar: 0,
  ja: 598,
  zh: 22,
  'zh-hant': 598
}

/**
 * Ceiling on keys a locale still defines that `en` no longer has. These are
 * dead strings: renamed or deleted upstream, left behind in the translation.
 * `tsc` catches most of them, but not under the `Record<string, string>`
 * members of `Translations` (the settings field copy), which accept any key.
 */
const MAX_UNUSED: Record<Locale, number> = {
  en: 0,
  ar: 5,
  ja: 0,
  zh: 19,
  'zh-hant': 0
}

function leafKeys(value: unknown, prefix = '', out = new Set<string>()): Set<string> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    if (prefix) {
      out.add(prefix)
    }

    return out
  }

  for (const [key, child] of Object.entries(value)) {
    leafKeys(child, prefix ? `${prefix}.${key}` : key, out)
  }

  return out
}

const ENGLISH_KEYS = leafKeys(en)

/**
 * What the locale wrote, not what it renders: for a partial locale that is the
 * override object, for a complete `Translations` literal it is the locale
 * itself.
 */
function authoredKeys(locale: Locale): Set<string> {
  const translations = TRANSLATIONS[locale]

  return leafKeys(authoredStrings(translations) ?? translations)
}

describe('desktop locale coverage', () => {
  it('has an English catalogue to measure against', () => {
    expect(ENGLISH_KEYS.size).toBeGreaterThan(0)
  })

  for (const { id } of LOCALE_OPTIONS) {
    it(`${id} leaves at most ${MAX_UNTRANSLATED[id]} strings untranslated`, () => {
      const authored = authoredKeys(id)
      const untranslated = [...ENGLISH_KEYS].filter(key => !authored.has(key))

      expect(
        untranslated.length,
        `${id} is missing ${untranslated.length} of ${ENGLISH_KEYS.size} English strings, which will render in English. ` +
          `Translate them in ${id}.ts, or raise the ceiling only with a reason. First few: ${untranslated
            .slice(0, 8)
            .join(', ')}`
      ).toBeLessThanOrEqual(MAX_UNTRANSLATED[id])
    })

    it(`${id} carries at most ${MAX_UNUSED[id]} strings English no longer uses`, () => {
      const unused = [...authoredKeys(id)].filter(key => !ENGLISH_KEYS.has(key))

      expect(
        unused.length,
        `${id} defines ${unused.length} keys absent from en.ts — dead strings to delete. First few: ${unused
          .slice(0, 8)
          .join(', ')}`
      ).toBeLessThanOrEqual(MAX_UNUSED[id])
    })
  }
})
