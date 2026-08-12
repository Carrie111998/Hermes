import { describe, expect, it } from 'vitest'

import { normalizeHermesOpenString, pathFromOpenDeepLink, resolveHermesOpenPath } from './hermes-open-target'

describe('normalizeHermesOpenString', () => {
  it('accepts hash-router paths and strips a leading hash', () => {
    expect(normalizeHermesOpenString('/kanban?task=t1')).toBe('/kanban?task=t1')
    expect(normalizeHermesOpenString('#/kanban?task=t1')).toBe('/kanban?task=t1')
  })

  it('maps hermes://open/… deep links to the same path', () => {
    expect(normalizeHermesOpenString('hermes://open/kanban?task=t1')).toBe('/kanban?task=t1')
    expect(normalizeHermesOpenString('hermes://open/settings/plugins')).toBe('/settings/plugins')
  })

  it('rejects non-open hermes kinds and unsafe paths', () => {
    expect(normalizeHermesOpenString('hermes://blueprint/morning-brief')).toBeNull()
    expect(normalizeHermesOpenString('https://example.com/x')).toBeNull()
    expect(normalizeHermesOpenString('/../etc/passwd')).toBeNull()
    expect(normalizeHermesOpenString('kanban')).toBeNull()
  })
})

describe('resolveHermesOpenPath', () => {
  it('merges structured path + params', () => {
    expect(resolveHermesOpenPath({ path: '/kanban', params: { task: 't1', board: 'default' } })).toBe(
      '/kanban?task=t1&board=default'
    )
  })

  it('resolves href the same as a bare string', () => {
    expect(resolveHermesOpenPath({ href: 'hermes://open/kanban?task=t1' })).toBe('/kanban?task=t1')
  })
})

describe('pathFromOpenDeepLink', () => {
  it('builds the navigate path from a deep-link payload', () => {
    expect(pathFromOpenDeepLink('kanban', { task: 't1' })).toBe('/kanban?task=t1')
    expect(pathFromOpenDeepLink('agent/42')).toBe('/agent/42')
  })
})
