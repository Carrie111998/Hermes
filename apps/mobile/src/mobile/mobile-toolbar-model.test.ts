import { describe, expect, it, vi } from 'vitest'

import { mobileToolbarContextActions } from './mobile-toolbar-model'

describe('mobileToolbarContextActions', () => {
  it('keeps every visible desktop titlebar action reachable through the mobile overflow, including routes, callbacks, and external links', () => {
    const refresh = vi.fn()
    const tools = [
      { icon: null, id: 'refresh', label: 'Refresh preview', onSelect: refresh },
      { href: 'https://docs.example.test', icon: null, id: 'docs', label: 'Open docs' },
      { icon: null, id: 'terminal', label: 'Terminal', to: '/terminal' },
      { hidden: true, icon: null, id: 'hidden', label: 'Hidden desktop action' }
    ]

    expect(mobileToolbarContextActions(tools)).toEqual([
      { id: 'refresh', label: 'Refresh preview', onSelect: refresh },
      { href: 'https://docs.example.test', id: 'docs', label: 'Open docs' },
      { id: 'terminal', label: 'Terminal', to: '/terminal' }
    ])
  })

  it('does not show the same desktop action twice when contributors overlap', () => {
    expect(
      mobileToolbarContextActions([
        { icon: null, id: 'preview', label: 'Preview' },
        { icon: null, id: 'preview', label: 'Preview again' }
      ])
    ).toEqual([{ id: 'preview', label: 'Preview' }])
  })
})
