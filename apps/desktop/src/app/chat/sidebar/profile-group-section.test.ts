import { describe, expect, it } from 'vitest'

import type { SidebarSessionGroup } from './projects'
import { groupProfileGroupsByParent } from './sessions-section'

const profileGroup = (id: string, parentGroup: null | string, sessions: string[]): SidebarSessionGroup => ({
  color: null,
  id,
  label: id,
  mode: 'profile',
  parentGroup,
  path: null,
  sessions: sessions.map(sessionId => ({ id: sessionId })) as SidebarSessionGroup['sessions']
})

describe('groupProfileGroupsByParent (group → profile tree)', () => {
  it('returns an empty tree for no groups', () => {
    expect(groupProfileGroupsByParent([])).toEqual([])
  })

  it('keeps a flat list when no profile names a parent group', () => {
    const groups = [profileGroup('alpha', null, ['s1']), profileGroup('beta', null, ['s2'])]

    const tree = groupProfileGroupsByParent(groups)

    expect(tree).toEqual([{ name: null, groups }])
  })

  it('nests profiles under their named parent groups, ungrouped last', () => {
    const teamA = [profileGroup('alpha', 'Team A', ['s1']), profileGroup('beta', 'Team A', ['s2'])]
    const teamB = [profileGroup('gamma', 'Team B', ['s3'])]
    const ungrouped = [profileGroup('delta', null, ['s4'])]

    const tree = groupProfileGroupsByParent([...teamA, ...ungrouped, ...teamB])

    expect(tree.map(section => section.name)).toEqual(['Team A', 'Team B', null])
    expect(tree[0].groups).toEqual(teamA)
    expect(tree[1].groups).toEqual(teamB)
    expect(tree[2].groups).toEqual(ungrouped)
  })

  it('preserves first-seen order for both parent groups and members', () => {
    const groups = [
      profileGroup('beta', 'Team A', ['s2']),
      profileGroup('alpha', 'Team A', ['s1']),
      profileGroup('delta', 'Team B', ['s4']),
      profileGroup('gamma', 'Team B', ['s3'])
    ]

    const tree = groupProfileGroupsByParent(groups)

    expect(tree.map(section => section.name)).toEqual(['Team A', 'Team B'])
    expect(tree[0].groups.map(group => group.id)).toEqual(['beta', 'alpha'])
    expect(tree[1].groups.map(group => group.id)).toEqual(['delta', 'gamma'])
  })

  it('omits the ungrouped section when every profile is grouped', () => {
    const groups = [profileGroup('alpha', 'Team A', ['s1']), profileGroup('gamma', 'Team B', ['s3'])]

    const tree = groupProfileGroupsByParent(groups)

    expect(tree.map(section => section.name)).toEqual(['Team A', 'Team B'])
  })
})
