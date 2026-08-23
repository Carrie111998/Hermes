import { describe, expect, it } from 'vitest'

import { TRANSLATIONS } from './catalog'
import { authoredStrings, type TranslationOverrides } from './define-locale'
import { en } from './en'
import { LOCALE_OPTIONS } from './languages'
import type { Locale, Translations } from './types'

/**
 * Exact baseline for English strings a locale intentionally leaves untranslated.
 * Exact counts prevent improvements from silently creating regression headroom;
 * update a baseline only when the intentional debt changes, and explain why.
 */
const EXPECTED_UNTRANSLATED: Record<Locale, number> = {
  en: 0,
  ar: 0,
  ja: 598,
  zh: 22,
  'zh-hant': 598
}

/** Exact baseline for locale keys that English no longer defines. */
const EXPECTED_UNUSED: Record<Locale, number> = {
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

/** Measure what a locale authored, never the object already merged over English. */
function authoredEntries(locale: Locale): Translations | TranslationOverrides {
  const translations = TRANSLATIONS[locale]

  return authoredStrings(translations) ?? translations
}

function authoredKeys(locale: Locale): Set<string> {
  return leafKeys(authoredEntries(locale))
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

type ParsedArrow = {
  params: string[]
  body: string
}

/**
 * Parse runtime arrow-function parameters instead of using `Function.length`,
 * which loses information after default/rest parameters. Unsupported parameter
 * shapes fail the guard instead of being silently skipped.
 */
function parseArrow(fn: (...args: never[]) => string): ParsedArrow | null {
  const source = fn.toString()
  const arrow = source.indexOf('=>')

  if (arrow < 0) {
    return null
  }

  let head = source.slice(0, arrow).trim()

  if (head.startsWith('(') && head.endsWith(')')) {
    head = head.slice(1, -1)
  }

  const params = head
    .split(',')
    .map(part => part.trim())
    .filter(Boolean)
    .map(part => part.replace(/^\.\.\./, '').split(/[:=]/)[0].trim())

  if (params.some(param => !/^[$A-Z_a-z][$\w]*$/.test(param))) {
    return null
  }

  return { params, body: source.slice(arrow + 2) }
}

/** Strip comments/literal text while retaining `${...}` template expressions. */
function searchableBody(body: string): string {
  const withoutCommentsAndStrings = body
    .replace(/\/\*[\s\S]*?\*\//g, ' ')
    .replace(/\/\/[^\n\r]*/g, ' ')
    .replace(/'(?:\\.|[^'\\])*'/g, "''")
    .replace(/"(?:\\.|[^"\\])*"/g, '""')

  return withoutCommentsAndStrings.replace(/`(?:\\.|[^`\\])*`/gs, template => {
    return [...template.matchAll(/\$\{([^}]*)\}/g)].map(match => match[1]).join(' ')
  })
}

function referenced(body: string, param: string): boolean {
  return new RegExp(`(^|[^A-Za-z0-9_$])${param}([^A-Za-z0-9_$]|$)`).test(searchableBody(body))
}

const ENGLISH_ENTRIES = leafEntries(en)

describe('desktop locale coverage', () => {
  it('has an English catalogue to measure against', () => {
    expect(ENGLISH_KEYS.size).toBeGreaterThan(0)
  })

  for (const { id } of LOCALE_OPTIONS) {
    it(`${id} matches its untranslated-string baseline of ${EXPECTED_UNTRANSLATED[id]}`, () => {
      const authored = authoredKeys(id)
      const untranslated = [...ENGLISH_KEYS].filter(key => !authored.has(key))

      expect(
        untranslated.length,
        `${id} is missing ${untranslated.length} of ${ENGLISH_KEYS.size} English strings; baseline is ` +
          `${EXPECTED_UNTRANSLATED[id]}. Translate new gaps, or update the baseline only for intentional debt ` +
          `and explain why. First few: ${untranslated.slice(0, 8).join(', ')}`
      ).toBe(EXPECTED_UNTRANSLATED[id])
    })

    it(`${id} matches its unused-string baseline of ${EXPECTED_UNUSED[id]}`, () => {
      const unused = [...authoredKeys(id)].filter(key => !ENGLISH_KEYS.has(key))

      expect(
        unused.length,
        `${id} defines ${unused.length} keys absent from en.ts; baseline is ${EXPECTED_UNUSED[id]}. ` +
          `First few: ${unused.slice(0, 8).join(', ')}`
      ).toBe(EXPECTED_UNUSED[id])
    })

    it(`${id} keeps the English parameter count on every function entry`, () => {
      const authored = leafEntries(authoredEntries(id))
      const mismatched: string[] = []
      const unparsed: string[] = []

      for (const [key, englishValue] of ENGLISH_ENTRIES) {
        const localeValue = authored.get(key)

        if (typeof englishValue !== 'function' || typeof localeValue !== 'function') {
          continue
        }

        const englishArrow = parseArrow(englishValue as (...args: never[]) => string)
        const localeArrow = parseArrow(localeValue as (...args: never[]) => string)

        if (!englishArrow || !localeArrow) {
          unparsed.push(key)
          continue
        }

        if (englishArrow.params.length !== localeArrow.params.length) {
          mismatched.push(`${key} (en takes ${englishArrow.params.length}, ${id} takes ${localeArrow.params.length})`)
        }
      }

      expect(unparsed, `${id} has function entries the structural guard could not parse`).toEqual([])
      expect(
        mismatched,
        `${id} changes the parameter count of these entries, so a caller's argument can go nowhere`
      ).toEqual([])
    })

    it(`${id} uses every named parameter its function entries declare`, () => {
      const authored = leafEntries(authoredEntries(id))
      const ignored: string[] = []
      const unparsed: string[] = []

      for (const [key, localeValue] of authored) {
        if (typeof localeValue !== 'function' || typeof ENGLISH_ENTRIES.get(key) !== 'function') {
          continue
        }

        const parsed = parseArrow(localeValue as (...args: never[]) => string)

        if (!parsed) {
          unparsed.push(key)
          continue
        }

        // Leading underscore is the established intentional-unused convention.
        const dead = parsed.params.filter(param => !param.startsWith('_') && !referenced(parsed.body, param))

        if (dead.length) {
          ignored.push(`${key} [${dead.join(', ')}]`)
        }
      }

      expect(unparsed, `${id} has function entries the structural guard could not parse`).toEqual([])
      expect(
        ignored,
        `${id} declares parameters it never uses, so caller data can be dropped from the translation`
      ).toEqual([])
    })

    it(`${id} keeps authored array-valued entries the same length as English`, () => {
      const authored = leafEntries(authoredEntries(id))
      const mismatched: string[] = []

      for (const [key, englishValue] of ENGLISH_ENTRIES) {
        if (!Array.isArray(englishValue)) {
          continue
        }

        const localeValue = authored.get(key)

        // Missing keys belong to the coverage baseline above. Shape checks only
        // apply once this locale actually authors the array-valued entry.
        if (localeValue === undefined) {
          continue
        }

        if (!Array.isArray(localeValue)) {
          mismatched.push(`${key} (en is an array, ${id} is not)`)
          continue
        }

        if (englishValue.length !== localeValue.length) {
          mismatched.push(`${key} (en has ${englishValue.length}, ${id} has ${localeValue.length})`)
        }
      }

      expect(mismatched, `${id} changes the shape of these array-valued translation entries`).toEqual([])
    })
  }
})
