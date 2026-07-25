import { describe, expect, it } from 'vitest'

// We test the catalogue directly (not via the React provider) so the
// assertions are cheap and survive unrelated changes to context plumbing.
// Mirrors the builtin set declared in `lib/chat-runtime.ts:BUILTIN_PERSONALITIES`,
// kept verbatim here so a missing translation is caught at test time even
// before the dropdown ever renders.
const BUILTIN_PERSONALITIES = [
  'helpful',
  'concise',
  'technical',
  'creative',
  'teacher',
  'kawaii',
  'catgirl',
  'pirate',
  'shakespeare',
  'surfer',
  'noir',
  'uwu',
  'philosopher',
  'hype'
] as const

import { TRANSLATIONS } from './catalog'
import type { Locale } from './types'

describe('desktop i18n — builtin personality labels', () => {
  // Every builtin ID the renderer can show in the `display.personality`
  // dropdown must have a Simplified Chinese label, otherwise zh users still
  // see the raw English ID in the dropdown.
  it('zh provides a localized label for every builtin personality', () => {
    const labels = TRANSLATIONS.zh.settings.personalities

    for (const id of BUILTIN_PERSONALITIES) {
      const label = labels[id]
      expect(label, `missing zh label for builtin personality "${id}"`).toBeTypeOf('string')
      expect((label ?? '').trim().length, `zh label for "${id}" is empty`).toBeGreaterThan(0)
      // Defence in depth: a label that is identical to the raw ID means the
      // translator forgot this entry — surface it loudly instead of silently
      // shipping English in the Simplified Chinese dropdown.
      expect(label, `zh label for "${id}" is just the raw ID`).not.toBe(id)
    }
  })

  it('zh-hant mirrors zh coverage so 繁體 dropdown is not partially English', () => {
    const labels = TRANSLATIONS['zh-hant'].settings.personalities
    const zhLabels = TRANSLATIONS.zh.settings.personalities

    for (const id of BUILTIN_PERSONALITIES) {
      const label = labels[id]
      expect(label, `missing zh-hant label for builtin personality "${id}"`).toBeTypeOf('string')
      expect((label ?? '').trim().length, `zh-hant label for "${id}" is empty`).toBeGreaterThan(0)
      expect(label, `zh-hant label for "${id}" is just the raw ID`).not.toBe(id)
    }

    // The Simplified and Traditional sets should cover exactly the same keys.
    expect(Object.keys(labels).sort()).toEqual(Object.keys(zhLabels).sort())
  })

  it('every locale exposes the personalities map (may be empty for fallback)', () => {
    // Catalog completeness: the type system guarantees this, but assert at
    // runtime so a future locale addition can't ship without the field.
    const locales: Locale[] = ['en', 'zh', 'zh-hant', 'ja']

    for (const locale of locales) {
      const labels = TRANSLATIONS[locale].settings.personalities
      expect(labels, `${locale} missing settings.personalities`).toBeDefined()
      expect(typeof labels).toBe('object')
    }
  })

  it('en falls back to the raw builtin ID when no override exists', () => {
    // English UI users see the raw personality ID (helpful, concise, …) in
    // the dropdown today. Documenting that contract here keeps the contract
    // intentional: any future `en.personalities` map is an additive change.
    const labels = TRANSLATIONS.en.settings.personalities

    for (const id of BUILTIN_PERSONALITIES) {
      const label = labels[id] ?? id
      expect(label).toBe(id)
    }
  })
})