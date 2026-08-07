import { describe, expect, it } from 'vitest'

import { orderProjectsByIds, sortProjectsByLabel, sortProjectsForOverview } from './model'
import { NO_PROJECT_ID, type SidebarProjectTree } from './workspace-groups'

function makeProject(id: string, sessionCount: number): SidebarProjectTree {
  return {
    id,
    isAuto: true,
    label: id,
    lastActive: 0,
    path: `/repos/${id}`,
    previewSessions: [],
    repos: [],
    sessionCount
  }
}

const home = (): SidebarProjectTree => ({
  ...makeProject(NO_PROJECT_ID, 2),
  isAuto: false,
  isNoProject: true,
  path: null
})

const ids = (projects: SidebarProjectTree[]) => projects.map(project => project.id)

describe('orderProjectsByIds', () => {
  it('leaves the deterministic sort alone when nothing has been dragged', () => {
    const projects = [makeProject('a', 0), makeProject('b', 2)]

    expect(orderProjectsByIds(projects, [])).toBe(projects)
  })

  it('applies the saved manual order', () => {
    const projects = [makeProject('a', 1), makeProject('b', 1), makeProject('c', 1)]

    expect(ids(orderProjectsByIds(projects, ['c', 'a', 'b']))).toEqual(['c', 'a', 'b'])
  })

  it('keeps freshly-scanned zero-session repos below the hand-ordered list', () => {
    // The regression: a disk scan keeps finding git checkouts the user has
    // never opened in Hermes. Surfacing every unsaved id at the top buried the
    // projects they deliberately dragged into place.
    const projects = [makeProject('scanned-2', 0), makeProject('mine', 4), makeProject('scanned-1', 0)]

    expect(ids(orderProjectsByIds(projects, ['mine']))).toEqual(['mine', 'scanned-1', 'scanned-2'])
  })

  it('appends newly discovered active projects instead of jumping the manual order', () => {
    const projects = [makeProject('ordered', 1), makeProject('just-started', 3)]

    expect(ids(orderProjectsByIds(projects, ['ordered']))).toEqual(['ordered', 'just-started'])
  })

  it('drops ids that are no longer present', () => {
    const projects = [makeProject('a', 1)]

    expect(ids(orderProjectsByIds(projects, ['gone', 'a']))).toEqual(['a'])
  })

  it('keeps Home on top of a hand-picked order', () => {
    const projects = [makeProject('a', 1), home(), makeProject('b', 1)]

    expect(ids(orderProjectsByIds(projects, ['b', 'a']))).toEqual([NO_PROJECT_ID, 'b', 'a'])
  })
})

describe('sortProjectsForOverview', () => {
  it('puts Home first and sorts the remaining projects alphabetically', () => {
    const projects = [makeProject('zebra', 5), makeProject('Alpha', 0), home(), makeProject('beta', 2)]

    expect(ids(sortProjectsForOverview(projects))).toEqual([NO_PROJECT_ID, 'Alpha', 'beta', 'zebra'])
  })

  it('is unaffected by activity changes', () => {
    const before = [makeProject('zebra', 0), makeProject('alpha', 3)]

    const after = [
      { ...before[0], lastActive: 999, sessionCount: 8 },
      { ...before[1], lastActive: 1, sessionCount: 0 }
    ]

    expect(ids(sortProjectsForOverview(before))).toEqual(['alpha', 'zebra'])
    expect(ids(sortProjectsForOverview(after))).toEqual(['alpha', 'zebra'])
  })
})

describe('sortProjectsByLabel', () => {
  it('uses project id as a stable tie-breaker for duplicate names', () => {
    const first = { ...makeProject('p_1', 1), label: 'Demo' }
    const second = { ...makeProject('p_2', 1), label: 'demo' }

    expect(ids(sortProjectsByLabel([second, first]))).toEqual(['p_1', 'p_2'])
  })
})
