import { describe, expect, it } from 'vitest'

import { en } from './en'
import {
  DEFAULT_LOCALE,
  isLocale,
  isSupportedLocaleValue,
  LOCALE_OPTIONS,
  localeConfigValue,
  normalizeLocale
} from './languages'
import { pt } from './pt'

function flattenLocale(node: unknown, prefix = '', out = new Map<string, unknown>()) {
  if (typeof node === 'string' || typeof node === 'function' || Array.isArray(node)) {
    out.set(prefix, node)

    return out
  }

  if (node && typeof node === 'object') {
    for (const [key, value] of Object.entries(node as Record<string, unknown>)) {
      flattenLocale(value, prefix ? `${prefix}.${key}` : key, out)
    }
  }

  return out
}

const INTENTIONALLY_IDENTICAL_PT_BR_PATHS = [
  'artifacts.colLocationLink',
  'artifacts.itemsLink',
  'artifacts.kindLink',
  'artifacts.tabLinks',
  'artifacts.zero',
  'assistant.tool.prefixes.web',
  'composer.themeTryPost',
  'composer.url',
  'cron.deliveryLabels.discord',
  'cron.deliveryLabels.slack',
  'cron.deliveryLabels.telegram',
  'desktop.yoloTitle',
  'messaging.fieldCopy.MATRIX_HOMESERVER.placeholder',
  'messaging.fieldCopy.SIGNAL_HTTP_URL.placeholder',
  'modelPicker.pro',
  'notifications.native.turnDoneBody',
  'onboarding.pro',
  'preview.diff',
  'profiles.default',
  'profiles.env',
  'profiles.renameDescSuffix',
  'rightSidebar.terminal',
  'sendDiagnostics.links.discord',
  'settings.about.heading',
  'settings.appearance.installPlaceholder',
  'settings.config.attachmentSizeUnit',
  'settings.connections.kindCloud',
  'settings.connections.kindLocal',
  'settings.connections.kindSsh',
  'settings.connections.labelPlaceholder',
  'settings.gateway.cloudSignInTitle',
  'settings.gateway.cloudTitle',
  'settings.model.tasks.mcp.label',
  'settings.nav.gateway',
  'settings.nav.mcp',
  'settings.notifications.testTitle',
  'settings.plugins.agent.sources.entrypoint',
  'settings.plugins.agent.sources.git',
  'shell.approvalMode.manual',
  'shell.gatewayMenu.gateway',
  'shell.modelOptions.ultra',
  'shell.statusbar.contextUsagePanel.categories.mcp',
  'shell.statusbar.cron',
  'shell.statusbar.gateway',
  'shell.statusbar.gatewayTitle',
  'shell.statusbar.toggleTerminal',
  'shell.statusbar.webhooks',
  'sidebar.row.ageDay',
  'sidebar.row.ageHour',
  'sidebar.row.ageMin',
  'skills.sortAlpha',
  'skills.tabMcp',
  'webhooks.deliverOptions.discord',
  'webhooks.deliverOptions.slack',
  'webhooks.deliverOptions.telegram'
].sort()

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
    expect(normalizeLocale('pt')).toBe('pt-br')
    expect(normalizeLocale('PT-BR')).toBe('pt-br')
    expect(normalizeLocale(' português ')).toBe('pt-br')
  })

  it('offers Brazilian Portuguese once under its canonical locale id', () => {
    const portugueseOptions = LOCALE_OPTIONS.filter(locale => locale.englishName === 'Portuguese (Brazil)')

    expect(portugueseOptions).toHaveLength(1)
    expect(portugueseOptions[0]?.id).toBe('pt-br')
    expect(isLocale('pt')).toBe(false)
    expect(isLocale('pt-br')).toBe(true)
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

  it('localizes the composer context action in Brazilian Portuguese', () => {
    expect((pt.composer as Record<string, unknown>).addContext).toBe('Adicionar contexto')
  })

  it('overrides every top-level settings block instead of silently falling back to English', () => {
    const enSettings = en.settings as Record<string, unknown>
    const ptSettings = pt.settings as Record<string, unknown>
    const inheritedBlocks = Object.keys(enSettings).filter(key => ptSettings[key] === enSettings[key])

    expect(inheritedBlocks).toEqual([])
  })

  it('allows English fallback only for an explicit list of names, symbols, and identical PT-BR terms', () => {
    const english = flattenLocale(en)
    const portuguese = flattenLocale(pt)

    const inheritedPaths = [...english]
      .filter(([path, value]) => portuguese.get(path) === value)
      .map(([path]) => path)
      .sort()

    expect(inheritedPaths).toEqual(INTENTIONALLY_IDENTICAL_PT_BR_PATHS)
  })

  it('returns the persisted config value for supported locales', () => {
    expect(localeConfigValue('en')).toBe('en')
    expect(localeConfigValue('zh')).toBe('zh')
    expect(localeConfigValue('zh-hant')).toBe('zh-hant')
    expect(localeConfigValue('ja')).toBe('ja')
    expect(localeConfigValue('ar')).toBe('ar')
    expect(localeConfigValue('pt-br')).toBe('pt-br')
  })
})
