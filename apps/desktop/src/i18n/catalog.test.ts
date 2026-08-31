import { describe, expect, it, vi } from 'vitest'

import { createTranslationCatalog } from './catalog'
import type { Locale, Translations } from './types'

const translations = (label: string) => ({ settings: { language: { label } } }) as unknown as Translations

describe('createTranslationCatalog', () => {
  it('returns English synchronously without importing another locale', () => {
    const english = translations('Language')
    const importJa = vi.fn()
    const catalog = createTranslationCatalog(english, { ja: importJa })

    expect(catalog.get('en')).toBe(english)
    expect(catalog.get('ja')).toBe(english)
    expect(importJa).not.toHaveBeenCalled()
  })

  it('loads only the requested locale and deduplicates concurrent loads', async () => {
    const japanese = translations('言語')
    const importJa = vi.fn(async () => japanese)
    const importZh = vi.fn(async () => translations('语言'))
    const catalog = createTranslationCatalog(translations('Language'), { ja: importJa, zh: importZh })

    const [first, second] = await Promise.all([catalog.load('ja'), catalog.load('ja')])

    expect(first).toBe(japanese)
    expect(second).toBe(japanese)
    expect(catalog.get('ja')).toBe(japanese)
    expect(importJa).toHaveBeenCalledOnce()
    expect(importZh).not.toHaveBeenCalled()
  })

  it('does not re-request a locale module URL that Chromium has already poisoned', async () => {
    const failure = new Error('chunk failed')
    const importJa = vi.fn().mockRejectedValue(failure)
    const catalog = createTranslationCatalog(translations('Language'), { ja: importJa })

    await expect(catalog.load('ja')).rejects.toBe(failure)
    await expect(catalog.load('ja')).rejects.toBe(failure)
    expect(importJa).toHaveBeenCalledOnce()
  })

  it('rejects a locale without an importer instead of committing mixed-language state', async () => {
    const catalog = createTranslationCatalog(translations('Language'), {})

    await expect(catalog.load('ar' as Locale)).rejects.toThrow('No translation catalog for ar')
  })
})
