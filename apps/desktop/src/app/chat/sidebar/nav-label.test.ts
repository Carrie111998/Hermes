import { describe, expect, it } from 'vitest'

import { resolveSidebarNavLabel } from './nav-label'

describe('resolveSidebarNavLabel', () => {
  it('uses the localized Usage contract when sidebar copy has no Usage key', () => {
    expect(
      resolveSidebarNavLabel({
        fallback: '',
        id: 'usage',
        sidebarNav: {},
        usageLabel: '使用量面板'
      })
    ).toBe('使用量面板')
  })

  it('prefers sidebar-local copy for existing navigation entries', () => {
    expect(
      resolveSidebarNavLabel({
        fallback: 'Capabilities',
        id: 'skills',
        sidebarNav: { skills: '技能與工具' },
        usageLabel: '使用量面板'
      })
    ).toBe('技能與工具')
  })
})
