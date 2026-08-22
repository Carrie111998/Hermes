import { ar } from './ar'
import { en } from './en'
import { es } from './es'
import { ja } from './ja'
import type { Locale, Translations } from './types'
import { zh } from './zh'
import { zhHant } from './zh-hant'

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function deepMerge<T>(base: T, overrides: Partial<T>): T {
  if (!isRecord(base) || !isRecord(overrides)) {
    return (overrides ?? base) as T
  }

  const result: Record<string, unknown> = { ...base }

  for (const [key, value] of Object.entries(overrides)) {
    if (value === undefined) continue
    const baseValue = result[key]
    result[key] = isRecord(baseValue) && isRecord(value) ? deepMerge(baseValue, value) : value
  }

  return result as T
}

export const TRANSLATIONS: Record<Locale, Translations> = {
  en,
  es: deepMerge(en, es),
  zh,
  'zh-hant': zhHant,
  ja,
  ar
}
