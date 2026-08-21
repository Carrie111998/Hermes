import { describe, expect, it } from 'vitest'

import { zh } from '@/i18n/zh'
import type { MemoryProviderField } from '@/types/hermes'

import { localizeField, localizeFields } from './localize-provider'

// A Hindsight-ish field carrying the backend's English schema text.
function hindsightField(overrides: Partial<MemoryProviderField> = {}): MemoryProviderField {
  return {
    key: 'api_key',
    label: 'API key',
    kind: 'secret',
    value: '',
    description: 'Used to authenticate with the Hindsight API.',
    placeholder: 'Enter Hindsight API key',
    is_set: false,
    inline: true,
    group: 'Connection',
    options: [],
    ...overrides
  }
}

describe('localizeField', () => {
  it('overlays the active locale copy for a known provider field', () => {
    const field = localizeField('hindsight', hindsightField(), zh.settings.memoryProvider)

    expect(field.label).toBe('API 密钥')
    expect(field.description).toBe('用于通过 Hindsight API 进行身份验证。')
    expect(field.placeholder).toBe('输入 Hindsight API 密钥')
  })

  it('passes through unknown providers untouched', () => {
    const field = localizeField('honcho', hindsightField(), zh.settings.memoryProvider)

    expect(field.label).toBe('API key')
    expect(field.description).toBe('Used to authenticate with the Hindsight API.')
    expect(field.placeholder).toBe('Enter Hindsight API key')
  })

  it('leaves fields without a locale override alone', () => {
    const field = localizeField('hindsight', hindsightField({ key: 'api_url', label: 'API URL' }), zh.settings.memoryProvider)

    // No override registered beyond the ones in zh.ts — label falls back to schema text.
    expect(field.label).toBe('API URL')
  })

  it('translates select option labels for known option values', () => {
    const field = localizeField(
      'hindsight',
      hindsightField({
        key: 'mode',
        kind: 'select',
        label: 'Mode',
        options: [
          { value: 'cloud', label: 'Cloud', description: 'Hindsight Cloud API (lightweight, just needs an API key)' },
          { value: 'local_external', label: 'Local External', description: 'Connect to an existing Hindsight instance' }
        ]
      }),
      zh.settings.memoryProvider
    )

    expect(field.label).toBe('模式')
    expect(field.options[0].label).toBe('云端')
    expect(field.options[0].description).toContain('轻量')
    expect(field.options[1].label).toBe('本地外部')
  })

  it('does not mutate the schema field object', () => {
    const original = hindsightField()
    localizeField('hindsight', original, zh.settings.memoryProvider)

    expect(original.label).toBe('API key')
    expect(original.description).toBe('Used to authenticate with the Hindsight API.')
  })
})

describe('localizeFields', () => {
  it('maps every field of a config payload', () => {
    const fields = localizeFields(
      'hindsight',
      [hindsightField(), hindsightField({ key: 'bank_id', label: 'Bank ID' })],
      zh.settings.memoryProvider
    )

    expect(fields[0].label).toBe('API 密钥')
    expect(fields[1].label).toBe('Bank ID') // no zh override for bank_id
  })
})