// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'

import {
  DEFAULT_LOCALE,
  detectSystemLocale,
  isLocale,
  isSupportedLocaleValue,
  localeConfigValue,
  normalizeLocale,
  resolvePreferredLocale
} from './languages'

describe('desktop i18n languages', () => {
  it('normalizes supported locale aliases', () => {
    expect(normalizeLocale('en')).toBe('en')
    expect(normalizeLocale('EN-US')).toBe('en')
    expect(normalizeLocale('zh')).toBe('zh')
    expect(normalizeLocale('zh-CN')).toBe('zh')
    expect(normalizeLocale('zh-Hans')).toBe('zh')
    expect(normalizeLocale(' zh_hans_cn ')).toBe('zh')
    expect(normalizeLocale('zh-Hant')).toBe('zh-hant')
    expect(normalizeLocale('zh-TW')).toBe('zh-hant')
    expect(normalizeLocale('zh_HK')).toBe('zh-hant')
    expect(normalizeLocale('ja')).toBe('ja')
    expect(normalizeLocale('ja-JP')).toBe('ja')
    expect(normalizeLocale('ar')).toBe('ar')
    expect(normalizeLocale('AR-SA')).toBe('ar')
    expect(normalizeLocale(' ar_eg ')).toBe('ar')
  })

  it('falls back to English for empty or unsupported values', () => {
    expect(normalizeLocale(null)).toBe(DEFAULT_LOCALE)
    expect(normalizeLocale('')).toBe(DEFAULT_LOCALE)
    expect(normalizeLocale('de')).toBe(DEFAULT_LOCALE)
  })

  it('distinguishes exact locale ids from supported config aliases', () => {
    expect(isSupportedLocaleValue('zh-CN')).toBe(true)
    expect(isSupportedLocaleValue('zh-TW')).toBe(true)
    expect(isSupportedLocaleValue('ja-JP')).toBe(true)
    expect(isSupportedLocaleValue('de')).toBe(false)
    expect(isLocale('zh-CN')).toBe(false)
    expect(isLocale('zh')).toBe(true)
    expect(isLocale('zh-hant')).toBe(true)
    expect(isLocale('ja')).toBe(true)
    expect(isLocale('ar')).toBe(true)
  })

  it('returns the persisted config value for supported locales', () => {
    expect(localeConfigValue('en')).toBe('en')
    expect(localeConfigValue('zh')).toBe('zh')
    expect(localeConfigValue('zh-hant')).toBe('zh-hant')
    expect(localeConfigValue('ja')).toBe('ja')
    expect(localeConfigValue('ar')).toBe('ar')
  })

  it('picks the first supported system language and skips unsupported ones', () => {
    expect(detectSystemLocale(['zh-CN'])).toBe('zh')
    expect(detectSystemLocale(['zh-TW'])).toBe('zh-hant')
    expect(detectSystemLocale(['ja-JP', 'en-US'])).toBe('ja')
    expect(detectSystemLocale(['de-DE', 'zh-Hans'])).toBe('zh')
    expect(detectSystemLocale(['de-DE', 'fr-FR'])).toBe(DEFAULT_LOCALE)
    expect(detectSystemLocale([])).toBe(DEFAULT_LOCALE)
  })

  it('prefers an explicit locale, then a stored one, then the system locale', () => {
    const previous = window.localStorage.getItem('hermes-desktop.ui-locale')

    window.localStorage.setItem('hermes-desktop.ui-locale', 'ja')

    try {
      expect(resolvePreferredLocale('zh-CN')).toBe('zh')
      expect(resolvePreferredLocale(undefined)).toBe('ja')
    } finally {
      if (previous == null) {
        window.localStorage.removeItem('hermes-desktop.ui-locale')
      } else {
        window.localStorage.setItem('hermes-desktop.ui-locale', previous)
      }
    }
  })
})
