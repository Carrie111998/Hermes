import { describe, expect, it } from 'vitest'

import { shouldRequestInitialMobileCapabilities } from './initial-capabilities'

describe('shouldRequestInitialMobileCapabilities', () => {
  it('runs once for a newly connected installation and never nags after the request has been issued', () => {
    expect(shouldRequestInitialMobileCapabilities(null)).toBe(true)
    expect(shouldRequestInitialMobileCapabilities('requested')).toBe(false)
  })
})
