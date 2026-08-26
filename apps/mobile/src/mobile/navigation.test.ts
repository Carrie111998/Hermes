import { describe, expect, it } from 'vitest'

import { mobileBackDestination } from './navigation'

describe('mobileBackDestination', () => {
  it('returns login and token setup flows to the gateway screen', () => {
    expect(mobileBackDestination('login')).toBe('connect')
    expect(mobileBackDestination('token')).toBe('connect')
  })

  it('leaves the first gateway screen as the exit boundary', () => {
    expect(mobileBackDestination('connect')).toBeNull()
  })
})
