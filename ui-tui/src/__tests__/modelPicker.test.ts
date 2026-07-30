import { describe, expect, it } from 'vitest'

import {
  modelPickerPersistenceCopy,
  modelPickerSelectionArgument,
  providerIndexAfterClearingFilter
} from '../components/modelPicker.js'
import type { ModelOptionProvider } from '../gatewayTypes.js'

const provider = (slug: string, name = slug): ModelOptionProvider => ({ name, slug })

describe('ModelPicker provider filtering', () => {
  it('keeps the selected provider when clearing the provider filter', () => {
    const nous = provider('nous', 'Nous Portal')
    const ollama = provider('ollama-cloud', 'Ollama Cloud')

    const rows = [
      { name: nous.name, provider: nous },
      { name: ollama.name, provider: ollama }
    ]

    // With a provider-stage filter like "ollama", the selected row is index 0
    // in the filtered list, but index 1 in the full list after setFilter('').
    expect(providerIndexAfterClearingFilter(rows, ollama)).toBe(1)
  })

  it('returns -1 when provider is undefined', () => {
    const rows = [{ name: 'A', provider: provider('a') }]

    expect(providerIndexAfterClearingFilter(rows, undefined)).toBe(-1)
  })

  it('returns -1 when provider slug is not in rows', () => {
    const rows = [
      { name: 'A', provider: provider('a') },
      { name: 'B', provider: provider('b') }
    ]

    expect(providerIndexAfterClearingFilter(rows, provider('missing'))).toBe(-1)
  })

  it('returns -1 for empty rows', () => {
    expect(providerIndexAfterClearingFilter([], provider('a'))).toBe(-1)
  })

  it('finds the first match when multiple rows share a slug', () => {
    const p = provider('dup')

    const rows = [
      { name: 'First', provider: p },
      { name: 'Second', provider: p }
    ]

    expect(providerIndexAfterClearingFilter(rows, p)).toBe(0)
  })
})

describe('ModelPicker persistence target', () => {
  it('labels and emits durable delegation config without a fake session flag', () => {
    expect(modelPickerPersistenceCopy('delegation', false)).toBe('persist: delegation config')
    expect(modelPickerSelectionArgument('sub-model', 'custom:lab', 'delegation', false)).toBe(
      'sub-model --provider custom:lab'
    )
  })

  it('preserves the existing main-model session/global contract', () => {
    expect(modelPickerPersistenceCopy('model', false)).toBe('persist: session · ^g toggle')
    expect(modelPickerPersistenceCopy('model', true)).toBe('persist: global · ^g toggle')
    expect(modelPickerSelectionArgument('main-model', 'nous', 'model', false)).toBe(
      'main-model --provider nous --tui-session'
    )
    expect(modelPickerSelectionArgument('main-model', 'nous', 'model', true)).toBe(
      'main-model --provider nous --global'
    )
  })
})
