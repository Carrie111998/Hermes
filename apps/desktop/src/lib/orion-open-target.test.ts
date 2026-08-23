import { describe, expect, it } from 'vitest'

import {
  normalizeOrionOpenString,
  pathFromOrionDeepLink,
  pathFromOpenDeepLink,
  resolveOrionOpenPath
} from './orion-open-target'

describe('normalizeOrionOpenString', () => {
  it('accepts hash-router paths and strips a leading hash', () => {
    expect(normalizeOrionOpenString('/index-network/intent/1')).toBe('/index-network/intent/1')
    expect(normalizeOrionOpenString('#/index-network/intent/1')).toBe('/index-network/intent/1')
  })

  it('maps plugin-scoped orion:// deep links to the same path', () => {
    expect(normalizeOrionOpenString('orion://index-network/intent/1')).toBe('/index-network/intent/1')
    expect(normalizeOrionOpenString('orion://index-network/intent/1?focus=true')).toBe(
      '/index-network/intent/1?focus=true'
    )
  })

  it('maps orion://open/… deep links by stripping the open host', () => {
    expect(normalizeOrionOpenString('orion://open/index-network/intent/1')).toBe('/index-network/intent/1')
    expect(normalizeOrionOpenString('orion://open/settings/plugins')).toBe('/settings/plugins')
  })

  it('rejects reserved orion kinds and unsafe paths', () => {
    expect(normalizeOrionOpenString('orion://blueprint/morning-brief')).toBeNull()
    expect(normalizeOrionOpenString('orion://plugin/install')).toBeNull()
    expect(normalizeOrionOpenString('https://example.com/x')).toBeNull()
    expect(normalizeOrionOpenString('/../etc/passwd')).toBeNull()
    expect(normalizeOrionOpenString('index-network')).toBeNull()
  })
})

describe('resolveOrionOpenPath', () => {
  it('merges structured path + params', () => {
    expect(resolveOrionOpenPath({ path: '/index-network/intent/1', params: { focus: 'true' } })).toBe(
      '/index-network/intent/1?focus=true'
    )
  })

  it('resolves href the same as a bare string', () => {
    expect(resolveOrionOpenPath({ href: 'orion://index-network/intent/1' })).toBe('/index-network/intent/1')
  })
})

describe('pathFromOrionDeepLink', () => {
  it('builds the navigate path from a plugin-scoped deep-link payload', () => {
    expect(pathFromOrionDeepLink('index-network', 'intent/1')).toBe('/index-network/intent/1')
  })

  it('builds the navigate path from orion://open/… payloads', () => {
    expect(pathFromOpenDeepLink('index-network/intent/1')).toBe('/index-network/intent/1')
    expect(pathFromOrionDeepLink('open', 'agent/42')).toBe('/agent/42')
  })

  it('ignores reserved kinds', () => {
    expect(pathFromOrionDeepLink('blueprint', 'morning-brief')).toBeNull()
    expect(pathFromOrionDeepLink('plugin', 'install')).toBeNull()
  })
})
