// Minimal TUI i18n runtime — mirrors apps/desktop/src/i18n/{catalog,runtime,languages}
// Locale is resolved once from HERMES_LANG / LANG / LC_ALL, defaults to 'en'.

import { en } from './en.js'
import { ru } from './ru.js'
import type { Locale, Translations } from './types.js'

export type { Locale, Translations } from './types.js'
export { en } from './en.js'
export { ru } from './ru.js'

export const TRANSLATIONS: Record<Locale, Translations> = { en, ru }

export const DEFAULT_LOCALE: Locale = 'en'

const LOCALE_ALIASES: Record<string, Locale> = {
  en: 'en',
  'en-us': 'en',
  ru: 'ru',
  'ru-ru': 'ru',
  russian: 'ru',
  'русский': 'ru',
}

function normalize(value: string): string {
  return value.trim().toLowerCase().replace(/_/g, '-')
}

export function isLocale(v: unknown): v is Locale {
  return v === 'en' || v === 'ru'
}

export function normalizeLocale(value: unknown): Locale {
  if (typeof value !== 'string') return DEFAULT_LOCALE
  return LOCALE_ALIASES[normalize(value)] ?? DEFAULT_LOCALE
}

function detectLocale(): Locale {
  const raw =
    process.env.HERMES_LANG ??
    process.env.HERMES_LOCALE ??
    process.env.LANG ??
    process.env.LC_ALL ??
    ''
  // LANG is often "ru_RU.UTF-8" — strip encoding suffix
  const cleaned = raw.split('.')[0] ?? raw
  return normalizeLocale(cleaned)
}

let runtimeLocale: Locale = detectLocale()

export function getLocale(): Locale {
  return runtimeLocale
}

export function setLocale(locale: Locale): void {
  if (isLocale(locale)) runtimeLocale = locale
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

function resolvePath(source: unknown, key: string): unknown {
  return key.split('.').reduce<unknown>((cur, part) => (isRecord(cur) ? cur[part] : undefined), source)
}

function render(value: unknown, args: unknown[]): null | string {
  if (typeof value === 'string') return value
  if (typeof value === 'function') return (value as (...a: unknown[]) => string)(...args)
  return null
}

function translateFrom(source: (locale: Locale) => unknown, locale: Locale, key: string, args: unknown[]): string {
  const active = render(resolvePath(source(locale), key), args)
  if (active !== null) return active
  if (locale !== DEFAULT_LOCALE) {
    const fallback = render(resolvePath(source(DEFAULT_LOCALE), key), args)
    if (fallback !== null) return fallback
  }
  return key
}

/** Translate key for current locale. Usage: t('common.loading') or t('session.toolsCount', 3) */
export function t(key: string, ...args: unknown[]): string {
  return translateFrom(locale => TRANSLATIONS[locale], runtimeLocale, key, args)
}

/** Compatibility helper — same as desktop's translateNow */
export const translateNow = t

export function setRuntimeI18nLocale(locale: Locale): void {
  setLocale(locale)
}

export function getRuntimeI18nLocale(): Locale {
  return getLocale()
}
