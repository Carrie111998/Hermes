import { describe, expect, it } from 'vitest'

import { en } from './en'
import { ja } from './ja'
import { createManagedTranslations, sanitizeManagedBrandText } from './managed-brand'
import { zh } from './zh'
import { zhHant } from './zh-hant'

function collectRenderedCopy(value: unknown, copy: string[] = []): string[] {
  if (typeof value === 'string') {
    copy.push(value)

    return copy
  }

  if (typeof value === 'function') {
    copy.push(String(value('Example', '1', '2', '3')))

    return copy
  }

  if (value && typeof value === 'object' && !Array.isArray(value)) {
    Object.values(value).forEach(entry => collectRenderedCopy(entry, copy))
  }

  if (Array.isArray(value)) {
    value.forEach(entry => collectRenderedCopy(entry, copy))
  }

  return copy
}

describe('managed evaOS Agent branding', () => {
  it('rebrands visible upstream product and provider names', () => {
    expect(sanitizeManagedBrandText('Start Hermes Desktop with Eva and Nous Portal')).toBe(
      'Start evaOS Agent with evaOS Agent and Electric Sheep account'
    )
  })

  it('rebrands both static and parameterized translation copy without rewriting runtime values', () => {
    const managedEnglish = createManagedTranslations(en)
    const managedChinese = createManagedTranslations(zh)

    expect(managedEnglish.boot.ready).toBe('evaOS Agent is ready')
    expect(managedChinese.settings.gateway.connectedTo('Hermes-plugin', '1.0')).toBe(
      '已连接到 Hermes-plugin · evaOS Agent 1.0'
    )
  })

  it('keeps managed customer copy in all four shipped locales free of upstream product branding', () => {
    const visibleCopy = [en, ja, zh, zhHant].flatMap(locale => collectRenderedCopy(createManagedTranslations(locale)))

    const upstreamProductBrand =
      /\b(?:Hermes Desktop|Hermes Agent|Nous Portal|Nous Research|Eva by Electric Sheep|Hermes|Nous|Eva)\b/

    expect(visibleCopy.filter(value => upstreamProductBrand.test(value))).toEqual([])
    expect(visibleCopy.filter(value => /evaOS Agent agent/i.test(value))).toEqual([])
  })
})
