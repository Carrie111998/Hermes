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

const UPSTREAM_BRAND_RE =
  /Eva by Electric Sheep|Hermes Desktop|Hermes Agent|Nous Portal|Nous Research|\bHermes\b|\bEva\b|\bNous\b/

export function isManagedEvaosAgent(): boolean {
  return typeof window !== 'undefined' && Boolean(window.hermesDesktop?.eva)
}

export function sanitizeManagedBrandText(value: string): string {
  return BRAND_REPLACEMENTS.reduce((current, [pattern, replacement]) => current.replace(pattern, replacement), value)
}

function sanitizeFunctionResult(value: unknown, args: unknown[]): string {
  let rendered = String(value)

  const protectedArgs = args.filter(
    (arg, index, values): arg is string =>
      typeof arg === 'string' && UPSTREAM_BRAND_RE.test(arg) && values.indexOf(arg) === index
  )

  protectedArgs
    .sort((left, right) => right.length - left.length)
    .forEach((arg, index) => {
      rendered = rendered.split(arg).join(`\uE000${index}\uE001`)
    })

  rendered = sanitizeManagedBrandText(rendered)

  protectedArgs.forEach((arg, index) => {
    rendered = rendered.split(`\uE000${index}\uE001`).join(arg)
  })

  return rendered
}

function sanitizeCatalogValue(value: unknown): unknown {
  if (typeof value === 'string') {
    return sanitizeManagedBrandText(value)
  }

  if (typeof value === 'function') {
    return (...args: unknown[]) =>
      sanitizeFunctionResult((value as (...functionArgs: unknown[]) => unknown)(...args), args)
  }

  if (Array.isArray(value)) {
    return value.map(entry => sanitizeCatalogValue(entry))
  }

  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return Object.fromEntries(Object.entries(value).map(([key, entry]) => [key, sanitizeCatalogValue(entry)]))
  }

  return value
}

export function createManagedTranslations(translations: Translations): Translations {
  return sanitizeCatalogValue(translations) as Translations
}
