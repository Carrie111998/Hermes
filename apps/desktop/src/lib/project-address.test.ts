import { describe, expect, it } from 'vitest'

import {
  isProjectAddressKey,
  makeProjectAddress,
  parseProjectAddressKey,
  projectAddressKey,
  sameProjectAddress
} from './project-address'

describe('project-address', () => {
  it('round-trips connection + profile + backend id', () => {
    const addr = makeProjectAddress('mimir', 'work', 'p_abc')
    const key = projectAddressKey(addr)

    expect(isProjectAddressKey(key)).toBe(true)
    expect(parseProjectAddressKey(key)).toEqual({
      connectionId: 'mimir',
      profile: 'work',
      backendProjectId: 'p_abc'
    })
  })

  it('distinguishes same backend id on two gateways', () => {
    const a = makeProjectAddress('local', 'default', 'p_same')
    const b = makeProjectAddress('mimir', 'default', 'p_same')

    expect(projectAddressKey(a)).not.toBe(projectAddressKey(b))
    expect(sameProjectAddress(a, b)).toBe(false)
  })

  it('distinguishes same connection + id across profiles', () => {
    const a = makeProjectAddress('mimir', 'default', 'p_same')
    const b = makeProjectAddress('mimir', 'alt', 'p_same')

    expect(projectAddressKey(a)).not.toBe(projectAddressKey(b))
    expect(sameProjectAddress(a, b)).toBe(false)
  })

  it('leaves sentinels and bare ids unparsed', () => {
    expect(parseProjectAddressKey('__all_projects__')).toBeNull()
    expect(parseProjectAddressKey('__no_project__')).toBeNull()
    expect(parseProjectAddressKey('p_bare')).toBeNull()
    expect(isProjectAddressKey('p_bare')).toBe(false)
  })

  it('preserves path-shaped backend ids', () => {
    const addr = makeProjectAddress('local', 'default', '/Users/me/repo')
    expect(parseProjectAddressKey(projectAddressKey(addr))?.backendProjectId).toBe('/Users/me/repo')
  })
})
