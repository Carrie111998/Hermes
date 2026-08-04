import { ar } from './ar'
import { en } from './en'
import { ja } from './ja'
import { createManagedTranslations } from './managed-brand'
import type { Locale, Translations } from './types'
import { zh } from './zh'
import { zhHant } from './zh-hant'

export const TRANSLATIONS: Record<Locale, Translations> = {
  en,
  zh,
  'zh-hant': zhHant,
  ja,
  ar
}

export const MANAGED_TRANSLATIONS: Record<Locale, Translations> = {
  en: createManagedTranslations(en),
  zh: createManagedTranslations(zh),
  'zh-hant': createManagedTranslations(zhHant),
  ja: createManagedTranslations(ja),
  ar: createManagedTranslations(ar)
}
