import type { Translations } from './types'

const BRAND_REPLACEMENTS: ReadonlyArray<readonly [RegExp, string]> = [
  [/Eva by Electric Sheep/gi, 'evaOS Agent'],
  [/Hermes Desktop/gi, 'evaOS Agent'],
  [/Hermes Agent/gi, 'evaOS Agent'],
  [/Nous Portal/gi, 'Electric Sheep account'],
  [/Nous Research/gi, 'Electric Sheep'],
  [/\bHermes\b/gi, 'evaOS Agent'],
  [/\bEva\b/gi, 'evaOS Agent'],
  [/\bNous\b/gi, 'Electric Sheep']
]

const UPSTREAM_BRAND_RE =
  /Eva by Electric Sheep|Hermes Desktop|Hermes Agent|Nous Portal|Nous Research|\bHermes\b|\bEva\b|\bNous\b/i

const LEGAL_ATTRIBUTION = 'Hermes Agent by Nous Research'

export function isManagedEvaosAgent(): boolean {
  return typeof window !== 'undefined' && Boolean(window.hermesDesktop?.eva)
}

export function sanitizeManagedBrandText(value: string): string {
  const protectedValue = value.replaceAll(LEGAL_ATTRIBUTION, '\uE100LEGAL_ATTRIBUTION\uE101')

  const sanitized = BRAND_REPLACEMENTS.reduce(
    (current, [pattern, replacement]) => current.replace(pattern, replacement),
    protectedValue
  )

  return sanitized.replaceAll('\uE100LEGAL_ATTRIBUTION\uE101', LEGAL_ATTRIBUTION)
}

function isNousProviderIdentity(providerIdentity: null | string | undefined): boolean {
  const normalized = providerIdentity?.trim().toLowerCase() ?? ''

  return (
    normalized === 'nous' ||
    normalized === 'nous portal' ||
    normalized === 'nous research' ||
    normalized.startsWith('nous subscription')
  )
}

function sanitizeManagedProviderText(value: string): string {
  const trimmed = value.trim().toLowerCase()

  if (trimmed === 'subscription') {
    return 'managed'
  }

  return sanitizeManagedBrandText(
    value
      .replace(/Nous Subscription/gi, 'Electric Sheep managed service')
      .replace(/billed to your subscription/gi, 'included with your managed agent')
  )
}

/**
 * Sanitize backend-owned provider copy at customer display seams without
 * changing the provider identity sent back over the wire. Non-Nous providers,
 * unmanaged builds, and non-string values pass through unchanged.
 *
 * Function values are wrapped so translation/copy callbacks that interpolate
 * a backend label are sanitized after rendering as well.
 */
export function managedProviderDisplayValue<T>(
  providerIdentity: null | string | undefined,
  value: T,
  managed = isManagedEvaosAgent()
): T {
  if (!managed || !isNousProviderIdentity(providerIdentity)) {
    return value
  }

  if (typeof value === 'string') {
    return sanitizeManagedProviderText(value) as T
  }

  if (typeof value === 'function') {
    const render = value as (...args: unknown[]) => unknown

    return ((...args: unknown[]) => sanitizeManagedProviderText(String(render(...args)))) as T
  }

  return value
}

function sanitizeStringResult(value: string, args: unknown[]): string {
  let rendered = value

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

function sanitizeFunctionResult(value: unknown, args: unknown[]): unknown {
  if (typeof value === 'string') {
    return sanitizeStringResult(value, args)
  }

  if (Array.isArray(value)) {
    return value.map(entry => sanitizeFunctionResult(entry, args))
  }

  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([key, entry]) => [key, sanitizeFunctionResult(entry, args)]))
  }

  return value
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
