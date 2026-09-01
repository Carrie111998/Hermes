import { describe, expect, it, vi } from 'vitest'

import { createTranslationCatalog, type TranslationImporters, TRANSLATIONS } from './catalog'
import type { Locale, Translations } from './types'

const translations = (label: string) => ({ settings: { language: { label } } }) as unknown as Translations

const importers = (overrides: Partial<TranslationImporters> = {}): TranslationImporters => ({
  ar: async () => translations('ar'),
  ja: async () => translations('ja'),
  zh: async () => translations('zh'),
  'zh-hant': async () => translations('zh-hant'),
  ...overrides
})

describe('createTranslationCatalog', () => {
  it('returns English synchronously without importing another locale', () => {
    const english = translations('Language')
    const importJa = vi.fn()
    const catalog = createTranslationCatalog(english, importers({ ja: importJa }))

    expect(catalog.get('en')).toBe(english)
    expect(catalog.get('ja')).not.toBe(english)
    expect(catalog.get('ja')).toStrictEqual(english)
    expect(TRANSLATIONS.ja).not.toBe(TRANSLATIONS.en)
    expect(TRANSLATIONS.ja).toStrictEqual(TRANSLATIONS.en)
    expect(importJa).not.toHaveBeenCalled()
  })

  it('loads only the requested locale and deduplicates concurrent loads', async () => {
    const japanese = translations('言語')
    const importJa = vi.fn(async () => japanese)
    const importZh = vi.fn(async () => translations('语言'))
    const catalog = createTranslationCatalog(translations('Language'), importers({ ja: importJa, zh: importZh }))

    const [first, second] = await Promise.all([catalog.load('ja'), catalog.load('ja')])

    expect(first).toBe(japanese)
    expect(second).toBe(japanese)
    expect(catalog.get('ja')).toBe(japanese)
    expect(importJa).toHaveBeenCalledOnce()
    expect(importZh).not.toHaveBeenCalled()
  })

  it('allows a failed locale import to be retried after the failure settles', async () => {
    const failure = new Error('chunk failed')
    const japanese = translations('言語')
    const importJa = vi.fn().mockRejectedValueOnce(failure).mockResolvedValueOnce(japanese)
    const catalog = createTranslationCatalog(translations('Language'), importers({ ja: importJa }))

    await expect(catalog.load('ja')).rejects.toBe(failure)
    await expect(catalog.load('ja')).resolves.toBe(japanese)
    expect(importJa).toHaveBeenCalledTimes(2)
  })

  it('gives each unloaded locale an isolated English fallback', () => {
    const english = translations('Language')
    const catalog = createTranslationCatalog(english, importers({ ja: vi.fn() }))
    const fallback = catalog.get('ja')

    expect(fallback).not.toBe(english)
    ;(fallback.settings as unknown as { language: { label: string } }).language.label = 'Mutated fallback'

    expect((catalog.get('en').settings as unknown as { language: { label: string } }).language.label).toBe('Language')
    expect((catalog.get('ja').settings as unknown as { language: { label: string } }).language.label).toBe(
      'Mutated fallback'
    )
  })

  it('rejects a locale without an importer instead of committing mixed-language state', async () => {
    const catalog = createTranslationCatalog(translations('Language'), {} as never)

    await expect(catalog.load('ar' as Locale)).rejects.toThrow('No translation catalog for ar')
  })
})
