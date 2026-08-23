import { afterEach, describe, expect, it } from 'vitest'

import { registry } from '@/contrib/registry'

import {
  PROJECTS_PRESENTATION_AREA,
  presentProjects,
  type ProjectsPresentationContribution
} from './projects-presentation'
import type { SidebarProjectTree } from './projects'

const project = (id: string): SidebarProjectTree =>
  ({ id, label: id, path: `/work/${id}`, repos: [], sessionCount: 0, previewSessions: [] }) as SidebarProjectTree

const disposers: Array<() => void> = []
afterEach(() => {
  while (disposers.length) disposers.pop()?.()
})

function register(data: ProjectsPresentationContribution, order = 0) {
  disposers.push(registry.register({ area: PROJECTS_PRESENTATION_AREA, data, id: `provider-${order}`, order }))
}

describe('Projects presentation contribution', () => {
  it('returns null without a provider so the built-in flat list is unchanged', () => {
    expect(presentProjects([project('a')])).toBeNull()
  })

  it('groups known project ids and leaves unknown or unclaimed ids ungrouped', () => {
    register({ groups: [{ id: 'cue', label: 'CUE++', projectIds: ['a', 'missing'] }] })

    expect(presentProjects([project('a'), project('b')])).toEqual({
      groups: [{ collapsed: false, id: 'cue', label: 'CUE++', projects: [project('a')] }],
      ungrouped: [project('b')]
    })
  })

  it('assigns a project only once when provider data contains duplicates', () => {
    register({
      groups: [
        { id: 'one', label: 'One', projectIds: ['a'] },
        { id: 'two', label: 'Two', projectIds: ['a', 'b'], collapsed: true }
      ]
    })

    expect(presentProjects([project('a'), project('b')])?.groups).toEqual([
      { collapsed: false, id: 'one', label: 'One', projects: [project('a')] },
      { collapsed: true, id: 'two', label: 'Two', projects: [project('b')] }
    ])
  })

  it('uses only the first ordered provider', () => {
    register({ groups: [{ id: 'late', label: 'Late', projectIds: ['b'] }] }, 20)
    register({ groups: [{ id: 'early', label: 'Early', projectIds: ['a'] }] }, 10)

    expect(presentProjects([project('a'), project('b')])?.groups[0].id).toBe('early')
  })
})
