import { describe, expect, it } from 'vitest'

import { sessionIdentityKey } from '@/lib/session-identity'

import { isSessionPickerItemActive } from './session-picker'

describe('isSessionPickerItemActive', () => {
  it('marks only the owning profile active when stored ids collide', () => {
    const activeIdentity = sessionIdentityKey('shared', 'beta')

    expect(isSessionPickerItemActive({ id: 'shared', profile: 'alpha' }, activeIdentity)).toBe(false)
    expect(isSessionPickerItemActive({ id: 'shared', profile: 'beta' }, activeIdentity)).toBe(true)
  })
})
