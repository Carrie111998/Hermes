import { describe, expect, it } from 'vitest'

import { normalizeHermesOpenString, pathFromOpenDeepLink, resolveHermesOpenPath } from './hermes-open-target'

describe('normalizeHermesOpenString', () => {
  it('accepts hash-router paths and strips a leading hash', () => {
    expect(normalizeHermesOpenString('/my-page?item=i1')).toBe('/my-page?item=i1')
    expect(normalizeHermesOpenString('#/my-page?item=i1')).toBe('/my-page?item=i1')
  })

  it('maps hermes://open/… deep links to the same path', () => {
    expect(normalizeHermesOpenString('hermes://open/my-page?item=i1')).toBe('/my-page?item=i1')
    expect(normalizeHermesOpenString('hermes://open/settings/plugins')).toBe('/settings/plugins')
  })

  it('rejects non-open hermes kinds and unsafe paths', () => {
    expect(normalizeHermesOpenString('hermes://blueprint/morning-brief')).toBeNull()
    expect(normalizeHermesOpenString('https://example.com/x')).toBeNull()
    expect(normalizeHermesOpenString('/../etc/passwd')).toBeNull()
    expect(normalizeHermesOpenString('my-page')).toBeNull()
  })
})

describe('resolveHermesOpenPath', () => {
  it('merges structured path + params', () => {
    expect(resolveHermesOpenPath({ path: '/my-page', params: { item: 'i1', tab: 'detail' } })).toBe(
      '/my-page?item=i1&tab=detail'
    )
  })

  it('resolves href the same as a bare string', () => {
    expect(resolveHermesOpenPath({ href: 'hermes://open/my-page?item=i1' })).toBe('/my-page?item=i1')
  })
})

describe('pathFromOpenDeepLink', () => {
  it('builds the navigate path from a deep-link payload', () => {
    expect(pathFromOpenDeepLink('my-page', { item: 'i1' })).toBe('/my-page?item=i1')
    expect(pathFromOpenDeepLink('agent/42')).toBe('/agent/42')
  })
})
