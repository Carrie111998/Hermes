import { describe, expect, it } from 'vitest'

import { mobileWorkspacePanes } from './mobile-workspace-menu'

describe('mobileWorkspacePanes', () => {
  it('keeps every visible collapsible pane reachable while excluding hidden, undocked, and desktop-only panes', () => {
    const panes = [
      { data: { collapsible: true }, id: 'sessions', title: 'Sessions' },
      { data: { collapsible: true }, id: 'files', title: 'Files' },
      { data: { collapsible: true }, id: 'review', title: 'Review' },
      { data: { collapsible: true }, id: 'hidden-pane', title: 'Hidden' },
      { data: { collapsible: false }, id: 'workspace', title: 'Workspace' },
      { data: { collapsible: true }, id: 'not-in-layout', title: 'Detached' },
    ]

    expect(mobileWorkspacePanes(panes, new Set(['sessions', 'files', 'review', 'hidden-pane', 'workspace']), new Set(['hidden-pane']))).toEqual([
      { id: 'sessions', title: 'Sessions' },
      { id: 'files', title: 'Files' },
      { id: 'review', title: 'Review' },
    ])
  })
})
