import { en } from './en'
import type { Locale, Translations } from './types'

type TranslationImporter = () => Promise<Translations>
export type TranslationImporters = Record<Exclude<Locale, 'en'>, TranslationImporter>

export interface TranslationCatalog {
  /** Return the loaded catalog, or an isolated English fallback while loading. */
  get: (locale: Locale) => Translations
  load: (locale: Locale) => Promise<Translations>
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function cloneTranslationTree<T>(value: T): T {
  if (Array.isArray(value)) {
    return value.map(item => cloneTranslationTree(item)) as T
  }

  if (isRecord(value)) {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, cloneTranslationTree(item)])) as T
  }

  return value
}

export function createTranslationCatalog(
  english: Translations,
  importers: TranslationImporters
): TranslationCatalog {
  const loaded: Partial<Record<Locale, Translations>> = { en: english }
  const fallbacks: Partial<Record<Exclude<Locale, 'en'>, Translations>> = {}
  const pending: Partial<Record<Exclude<Locale, 'en'>, Promise<Translations>>> = {}

  const get = (locale: Locale) => {
    if (locale === 'en') {
      return english
    }

    const completed = loaded[locale]

    if (completed) {
      return completed
    }

    const fallback = fallbacks[locale]

    if (fallback) {
      return fallback
    }

    const isolatedFallback = cloneTranslationTree(english)
    fallbacks[locale] = isolatedFallback

    return isolatedFallback
  }

  const load = (locale: Locale): Promise<Translations> => {
    if (locale === 'en') {
      return Promise.resolve(english)
    }

    const completed = loaded[locale]

    if (completed) {
      return Promise.resolve(completed)
    }

    const inFlight = pending[locale]

    if (inFlight) {
      return inFlight
    }

    const importer = importers[locale]

    if (!importer) {
      return Promise.reject(new Error(`No translation catalog for ${locale}`))
    }

    const operation = Promise.resolve()
      .then(() => importer())
      .then(translations => {
        loaded[locale] = translations

        return translations
      })
      .finally(() => {
        delete pending[locale]
      })

    pending[locale] = operation

    return operation
  }

  return { get, load }
}

const localeImporters = {
  ar: () => import('./ar').then(module => module.ar),
  ja: () => import('./ja').then(module => module.ja),
  zh: () => import('./zh').then(module => module.zh),
  'zh-hant': () => import('./zh-hant').then(module => module.zhHant)
} satisfies Record<Exclude<Locale, 'en'>, TranslationImporter>

const catalog = createTranslationCatalog(en, localeImporters)

export const getTranslations = catalog.get
export const loadTranslations = catalog.load

/**
 * Backwards-compatible synchronous view of the locale catalogs. A locale that
 * has not finished loading returns a detached English fallback; loading it
 * replaces that view with the real catalog without sharing mutable trees with
 * English.
 */
export const TRANSLATIONS: Record<Locale, Translations> = {
  get en() {
    return getTranslations('en')
  },
  get zh() {
    return getTranslations('zh')
  },
  get 'zh-hant'() {
    return getTranslations('zh-hant')
  },
  get ja() {
    return getTranslations('ja')
  },
  get ar() {
    return getTranslations('ar')
  }
}
