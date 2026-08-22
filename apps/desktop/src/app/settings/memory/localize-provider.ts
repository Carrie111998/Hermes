/**
 * Render-layer localization for backend-provided memory provider config
 * surfaces (Hindsight & friends). The backend schema text is shared by every
 * language, so it stays English; these helpers overlay per-provider overrides
 * from the active locale bundle at render time — without touching the schema.
 */

import type { Translations } from '@/i18n/types'
import type { MemoryProviderField, MemoryProviderFieldOption } from '@/types/hermes'

type ProviderCopy = Translations['settings']['memoryProvider']

/** Overlay locale overrides onto a backend-provided field (label/description/
 *  placeholder/option labels). Returns a new object; the schema field is untouched. */
export function localizeField(
  provider: string,
  field: MemoryProviderField,
  copy: ProviderCopy
): MemoryProviderField {
  const key = `${provider}.${field.key}`
  const label = copy.fieldOverrides[key] ?? field.label
  const description = copy.descOverrides[key] ?? field.description
  const placeholder = copy.placeholderOverrides[key] ?? field.placeholder

  let options = field.options
  if (options && options.length > 0) {
    options = options.map((option: MemoryProviderFieldOption) => {
      const optionLabel = copy.optionOverrides[`${key}.${option.value}`] ?? option.label
      const optionDesc = copy.optionOverrides[`${key}.${option.value}.desc`] ?? option.description
      return optionLabel === option.label && optionDesc === option.description
        ? option
        : { ...option, label: optionLabel, description: optionDesc }
    })
  }

  if (label === field.label && description === field.description && placeholder === field.placeholder && options === field.options) {
    return field
  }

  return { ...field, label, description, placeholder, options }
}

/** Apply `localizeField` to every field of a config payload. */
export function localizeFields(
  provider: string,
  fields: MemoryProviderField[],
  copy: ProviderCopy
): MemoryProviderField[] {
  return fields.map(field => localizeField(provider, field, copy))
}