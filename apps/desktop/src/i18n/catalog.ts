import { en } from './en'
import type { Locale, Translations } from './types'

type TranslationImporter = () => Promise<Translations>
type TranslationImporters = Partial<Record<Exclude<Locale, 'en'>, TranslationImporter>>

export interface TranslationCatalog {
  get: (locale: Locale) => Translations
  load: (locale: Locale) => Promise<Translations>
}

export function createTranslationCatalog(
  english: Translations,
  importers: TranslationImporters
): TranslationCatalog {
  const loaded: Partial<Record<Locale, Translations>> = { en: english }
  const pending: Partial<Record<Locale, Promise<Translations>>> = {}

  const get = (locale: Locale) => loaded[locale] ?? english

  const load = (locale: Locale): Promise<Translations> => {
    const completed = loaded[locale]

    if (completed) {
      return Promise.resolve(completed)
    }

    const inFlight = pending[locale]

    if (inFlight) {
      return inFlight
    }

    const importer = locale === 'en' ? undefined : importers[locale]

    if (!importer) {
      return Promise.reject(new Error(`No translation catalog for ${locale}`))
    }

    const operation = importer().then(translations => {
      loaded[locale] = translations
      delete pending[locale]

      return translations
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
