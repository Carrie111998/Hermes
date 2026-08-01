import { describe, expect, it, vi } from 'vitest'

vi.mock('@hermes/plugin-sdk', () => ({
  atom: (value: unknown) => ({ get: () => value, set: () => undefined, listen: () => () => undefined }),
  queryClient: { invalidateQueries: () => Promise.resolve() },
  usePluginI18n: () => () => ''
}))

import {
  classOfServiceCreatePayload,
  classOfServiceOptions,
  classOfServicePatchPayload
} from './api'
import { classOfServiceLabel } from './i18n'

describe('Class of Service desktop contract', () => {
  it('uses backend-provided options and omits an unset create value', () => {
    const values = ['expedite', 'fixed_date', 'intangible', 'standard']
    expect(classOfServiceOptions({ classes_of_service: values })).toEqual(values)
    expect(classOfServiceCreatePayload('')).toEqual({})
    expect(classOfServiceCreatePayload('fixed_date')).toEqual({ class_of_service: 'fixed_date' })
  })

  it('sends exact update payloads and labels known plus future values', () => {
    expect(classOfServicePatchPayload(null)).toEqual({ class_of_service: null })
    expect(classOfServicePatchPayload('standard')).toEqual({ class_of_service: 'standard' })
    const labels = {
      classesOfService: {
        expedite: 'Expedite',
        fixed_date: 'Fixed date',
        intangible: 'Intangible',
        standard: 'Standard'
      }
    } as never
    expect(classOfServiceLabel(labels, 'expedite')).toBe('Expedite')
    expect(classOfServiceLabel(labels, 'future')).toBe('future')
  })
})
