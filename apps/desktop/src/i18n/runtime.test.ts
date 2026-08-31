import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { fieldCopyForSchemaKey } from '@/app/settings/field-copy'

import { loadTranslations, translationsFor } from './catalog'
import { setRuntimeI18nLocale, translateNow } from './runtime'
import { zh } from './zh'

describe('desktop i18n runtime translator', () => {
  // Non-English trees are separate chunks now (catalog.ts). The assertions
  // below are about translation content, so warm every locale first; the
  // not-yet-loaded path gets its own test at the bottom.
  beforeAll(async () => {
    await Promise.all([loadTranslations('ar'), loadTranslations('ja'), loadTranslations('zh'), loadTranslations('zh-hant')])
  })

  beforeEach(() => {
    setRuntimeI18nLocale('en')
  })

  afterEach(() => {
    setRuntimeI18nLocale('en')
  })

  it('translates string paths for the active runtime locale', () => {
    setRuntimeI18nLocale('zh')

    expect(translateNow('boot.ready')).toBe('Hermes 桌面版已就绪')
    expect(translateNow('notifications.voice.noSpeechDetected')).toBe('没有检测到语音')
    expect(translateNow('composer.lookupNoMatches')).toBe('没有匹配项。')
    expect(translateNow('assistant.tool.statusRecovered')).toBe('已恢复')
  })

  it('passes arguments to function translations', () => {
    expect(translateNow('notifications.updateReadyMessage', 2)).toBe('2 new changes available.')
  })

  it('translates migrated overlap keys for newly supported locales', () => {
    setRuntimeI18nLocale('ja')
    expect(translateNow('common.save')).toBe('保存')

    setRuntimeI18nLocale('zh-hant')
    expect(translateNow('cron.promptPlaceholder')).toBe('代理每次執行時應做什麼？')
  })

  it('translates settings copy for newly supported locales', () => {
    setRuntimeI18nLocale('ja')
    expect(translateNow('settings.appearance.title')).toBe('外観')
    expect(translateNow('settings.nav.providers')).toBe('プロバイダー')

    setRuntimeI18nLocale('zh-hant')
    expect(translateNow('settings.appearance.title')).toBe('外觀')
    expect(translateNow('settings.nav.providerApiKeys')).toBe('API 金鑰')

    setRuntimeI18nLocale('ar')
    expect(translateNow('settings.appearance.reasoningCollapsedTitle')).toBe('طي التفكير افتراضيًا')
    expect(translateNow('settings.appearance.reasoningCollapsedDesc')).toBe(
      'أبقِ التفكير المتدفق متاحًا دون توسيعه حتى تفتحه.'
    )
  })

  it('keeps translated settings field copy addressable from schema keys', () => {
    const field = ['display', 'show_reasoning'].join('.')

    expect(fieldCopyForSchemaKey(zh.settings.fieldLabels, field)).toBe('推理过程块')
    expect(fieldCopyForSchemaKey(zh.settings.fieldDescriptions, field)).toBe('当后端提供推理内容时予以显示。')
  })

  it('falls back to English when the active locale cannot resolve a key', () => {
    const boot = translationsFor('ja')!.boot as { ready?: string }
    const originalReady = boot.ready

    try {
      boot.ready = undefined
      setRuntimeI18nLocale('ja')

      expect(translateNow('boot.ready')).toBe('Hermes Desktop is ready')
    } finally {
      boot.ready = originalReady
    }
  })

  it('returns the key when no locale can resolve a path', () => {
    setRuntimeI18nLocale('zh')

    expect(translateNow('missing.path')).toBe('missing.path')
  })
})

// Own module registry: the suite above warms every locale in `beforeAll`, and
// the whole point here is the window BEFORE a locale's chunk has landed.
describe('desktop i18n runtime translator, before a locale chunk loads', () => {
  beforeEach(() => {
    vi.resetModules()
  })

  it('renders English rather than raw keys while the chunk is in flight', async () => {
    const runtime = await import('./runtime')

    runtime.setRuntimeI18nLocale('ja')

    // Japanese has not been fetched in this module registry, so the active
    // arm of translateFrom misses and the DEFAULT arm answers.
    expect(runtime.translateNow('boot.ready')).toBe('Hermes Desktop is ready')
  })

  it('serves the real translation once the chunk lands', async () => {
    const catalog = await import('./catalog')
    const runtime = await import('./runtime')

    runtime.setRuntimeI18nLocale('ja')
    await catalog.loadTranslations('ja')

    expect(runtime.translateNow('common.save')).toBe('保存')
  })

  it('shares one in-flight request between concurrent callers', async () => {
    const catalog = await import('./catalog')

    const [first, second] = await Promise.all([catalog.loadTranslations('ar'), catalog.loadTranslations('ar')])

    expect(first).toBeDefined()
    expect(second).toBe(first)
    expect(catalog.translationsFor('ar')).toBe(first)
  })
})
