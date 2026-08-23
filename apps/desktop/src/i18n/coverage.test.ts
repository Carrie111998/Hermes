import { describe, expect, it } from 'vitest'

import { TRANSLATIONS } from './catalog'
import { authoredStrings, type TranslationOverrides } from './define-locale'
import { en } from './en'
import { LOCALE_OPTIONS } from './languages'
import type { Locale, Translations } from './types'

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
 * without translating it pushes a locale past its ceiling and fails here. *
 * A ceiling is deliberately a number and not a hard 100% gate. If a locale wants
 * to defer a feature's strings, raise its ceiling in the same commit and say why
 * — an honest gap is worth more than English text pasted in to go green, which
 * looks translated to every tool and to the user until someone reports it.
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
 * The invariant this whole file rests on: measure what the locale **authored**,
 * never what it renders. `TRANSLATIONS[locale]` has already been merged over
 * `en`, so comparing that to `en` reports full coverage for every locale and
 * catches nothing — which is exactly how the drift this guard exists for went
 * unnoticed. `authoredStrings` returns the pre-merge overrides; the fallback is
 * for a locale declared as a complete `Translations` literal, which authors its
 * keys directly and so is its own answer.
 */
function authoredEntries(locale: Locale): Translations | TranslationOverrides {
  const translations = TRANSLATIONS[locale]

  return authoredStrings(translations) ?? translations
}

function authoredKeys(locale: Locale): Set<string> {
  return leafKeys(authoredEntries(locale))
}

/** Parameter names and body text of an arrow function, from its source. */
function signature(fn: (...args: never[]) => string): { params: string[]; body: string } | null {
  const source = fn.toString()
  const arrow = source.indexOf('=>')

  if (arrow < 0) {
    return null
  }

  let head = source.slice(0, arrow).trim()

  if (head.startsWith('(') && head.endsWith(')')) {
    head = head.slice(1, -1)
  }

  return {
    params: head
      .split(',')
      .map(part => part.trim().split(/[:=]/)[0].trim())
      .filter(Boolean),
    body: source.slice(arrow + 2)
  }
}

/** Whether `param` appears as a standalone identifier in `body`. */
function referenced(body: string, param: string): boolean {
  return new RegExp(`(^|[^A-Za-z0-9_$])${param}([^A-Za-z0-9_$]|$)`).test(body)
}

function leafEntries(value: unknown, prefix = '', out = new Map<string, unknown>()): Map<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    if (prefix) {
      out.set(prefix, value)
    }

    return out
  }

  for (const [key, child] of Object.entries(value)) {
    leafEntries(child, prefix ? `${prefix}.${key}` : key, out)
  }

  return out
}

const ENGLISH_ENTRIES = leafEntries(en)

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

    // Key presence is not enough. A translated entry that keeps the key but
    // drops the value the English entry interpolates renders `undefined` to the
    // user, and counting keys cannot see it. These are hard assertions rather
    // than ceilings: every locale satisfies them today.
    it(`${id} keeps the English parameter list on every function entry`, () => {
      const authored = leafEntries(authoredEntries(id))
      const mismatched: string[] = []

      for (const [key, englishValue] of ENGLISH_ENTRIES) {
        const localeValue = authored.get(key)

        if (typeof englishValue !== 'function' || typeof localeValue !== 'function') {
          continue
        }

        if (englishValue.length !== localeValue.length) {
          mismatched.push(`${key} (en takes ${englishValue.length}, ${id} takes ${localeValue.length})`)
        }
      }

      expect(
        mismatched,
        `${id} changes the parameter count of these entries, so a caller's argument goes nowhere`
      ).toEqual([])
    })

    it(`${id} uses every parameter its function entries declare`, () => {
      const authored = leafEntries(authoredEntries(id))
      const ignored: string[] = []

      for (const [key, localeValue] of authored) {
        if (typeof localeValue !== 'function' || typeof ENGLISH_ENTRIES.get(key) !== 'function') {
          continue
        }

        const parsed = signature(localeValue as (...args: never[]) => string)

        if (!parsed) {
          continue
        }

        // A leading underscore is the established way to declare a parameter
        // intentionally unused — en.ts does it too (see generatePet.hatchRow).
        const dead = parsed.params.filter(param => !param.startsWith('_') && !referenced(parsed.body, param))

        if (dead.length) {
          ignored.push(`${key} [${dead.join(', ')}]`)
        }
      }

      expect(
        ignored,
        `${id} declares parameters it never uses, so the value the caller passes is dropped from the string. ` +
          `Referencing a parameter in a condition counts — translating the word it selects is often better than ` +
          `interpolating the English token.`
      ).toEqual([])
    })
  }
})
