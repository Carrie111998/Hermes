import { describe, expect, it } from 'vitest'

import { ar } from './ar'
import { en } from './en'
import { ja } from './ja'
import { createManagedTranslations, managedProviderDisplayValue, sanitizeManagedBrandText } from './managed-brand'
import { zh } from './zh'
import { zhHant } from './zh-hant'

function collectRenderedCopy(value: unknown, copy: string[] = []): string[] {
  if (typeof value === 'string') {
    copy.push(value)

    return copy
  }

  if (typeof value === 'function') {
    const rendered = value('Example', '1', '2', '3')

    return collectRenderedCopy(rendered, copy)
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

  it('sanitizes managed Nous provider labels while preserving unmanaged and other-provider labels', () => {
    expect(managedProviderDisplayValue('nous', 'Nous Portal', true)).toBe('Electric Sheep account')
    expect(managedProviderDisplayValue('nous', 'nous subscription', true)).toBe('Electric Sheep managed service')
    expect(managedProviderDisplayValue('nous', 'nOuS pOrTaL', true)).toBe('Electric Sheep account')
    expect(managedProviderDisplayValue('Nous Subscription (Browser Use cloud)', 'subscription', true)).toBe('managed')
    expect(
      managedProviderDisplayValue(
        'Nous Subscription (Browser Use cloud)',
        'Managed Browser Use billed to your subscription',
        true
      )
    ).toBe('Managed Browser Use included with your managed agent')
    expect(managedProviderDisplayValue('nous', 'Nous Portal', false)).toBe('Nous Portal')
    expect(managedProviderDisplayValue('openai', 'Nous-compatible proxy', true)).toBe('Nous-compatible proxy')
  })

  it('sanitizes function-valued provider copy after dynamic backend labels are rendered', () => {
    const selectedMessage = managedProviderDisplayValue(
      'nous',
      (provider: string) => `Selected ${provider}; Hermes will use it.`,
      true
    )

    expect(selectedMessage('Nous Subscription')).toBe(
      'Selected Electric Sheep managed service; evaOS Agent will use it.'
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

  it('keeps managed customer copy in every shipped locale free of upstream product branding outside legal copy', () => {
    const visibleCopy = [en, ja, zh, zhHant, ar].flatMap(locale =>
      collectRenderedCopy(createManagedTranslations(locale))
    )

    const upstreamProductBrand =
      /\b(?:Hermes Desktop|Hermes Agent|Nous Portal|Nous Research|Eva by Electric Sheep|Hermes|Nous|Eva)\b/

    expect(
      visibleCopy.filter(value => !value.includes('Hermes Agent by Nous Research') && upstreamProductBrand.test(value))
    ).toEqual([])
    expect(visibleCopy.filter(value => /evaOS Agent agent/i.test(value))).toEqual([])
  })

  it('preserves object-valued translation results and legal attribution', () => {
    const managed = createManagedTranslations(en)

    expect(managed.sidebar.projects.branchOff()).toEqual({ after: '', before: 'branch off ' })
    expect(sanitizeManagedBrandText('Built on Hermes Agent by Nous Research, used under the MIT License.')).toBe(
      'Built on Hermes Agent by Nous Research, used under the MIT License.'
    )
  })
})
