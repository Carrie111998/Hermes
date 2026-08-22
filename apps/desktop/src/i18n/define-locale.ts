import { en } from './en'
import type { Translations } from './types'

type TranslationOverride<T> = T extends (...args: never[]) => string
  ? T
  : T extends readonly unknown[]
    ? T
    : T extends string
      ? string
      : T extends object
        ? { [K in keyof T]?: TranslationOverride<T[K]> }
        : T

export type TranslationOverrides = TranslationOverride<Translations>

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function mergeTranslations<T>(base: T, overrides: TranslationOverride<T> | undefined): T {
  if (!isRecord(base) || !isRecord(overrides)) {
    return (overrides ?? base) as T
  }

  const result: Record<string, unknown> = { ...base }

  for (const [key, value] of Object.entries(overrides)) {
    if (value === undefined) {
      continue
    }

    const baseValue = result[key]
    result[key] = isRecord(baseValue) && isRecord(value) ? mergeTranslations(baseValue, value) : value
  }

  return result as T
}

// `defineLocale` hands back a locale already merged over English, so at runtime
// a string nobody translated is indistinguishable from one that was. Keep a
// handle on what each locale actually authored so coverage stays measurable —
// see `coverage.test.ts`.
const AUTHORED = new WeakMap<Translations, TranslationOverrides>()

export function defineLocale(overrides: TranslationOverrides): Translations {
  const locale = mergeTranslations<Translations>(en, overrides)

  AUTHORED.set(locale, overrides)

  return locale
}

/**
 * The strings a locale supplied itself, before the English fallback was merged
 * in. `undefined` for a locale declared as a complete `Translations` literal,
 * which authors every key directly and so is its own answer.
 */
export function authoredStrings(locale: Translations): TranslationOverrides | undefined {
  return AUTHORED.get(locale)
}
