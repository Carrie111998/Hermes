import type { Translations } from './types'

const BRAND_REPLACEMENTS: ReadonlyArray<readonly [RegExp, string]> = [
  [/Eva by Electric Sheep/g, 'evaOS Agent'],
  [/Hermes Desktop/g, 'evaOS Agent'],
  [/Hermes Agent/g, 'evaOS Agent'],
  [/Nous Portal/g, 'Electric Sheep account'],
  [/Nous Research/g, 'Electric Sheep'],
  [/\bHermes\b/g, 'evaOS Agent'],
  [/\bEva\b/g, 'evaOS Agent'],
  [/\bNous\b/g, 'Electric Sheep']
]

export function isManagedEvaosAgent(): boolean {
  return typeof window !== 'undefined' && Boolean(window.hermesDesktop?.eva)
}

export function sanitizeManagedBrandText(value: string): string {
  return BRAND_REPLACEMENTS.reduce((current, [pattern, replacement]) => current.replace(pattern, replacement), value)
}

function sanitizeCatalogValue(value: unknown): unknown {
  if (typeof value === 'string') {
    return sanitizeManagedBrandText(value)
  }

  if (typeof value === 'function') {
    return value
  }

  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return Object.fromEntries(Object.entries(value).map(([key, entry]) => [key, sanitizeCatalogValue(entry)]))
  }

  return value
}

export function createManagedTranslations(translations: Translations): Translations {
  return sanitizeCatalogValue(translations) as Translations
}
