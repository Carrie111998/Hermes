import { beforeEach, describe, expect, it } from 'vitest'

import {
  $missingProjectPaths,
  $recentProjectRows,
  $recentProjects,
  clearRecentProjects,
  forgetRecentProject,
  isProjectMissing,
  markProjectMissing,
  MAX_RECENT_PROJECTS,
  recordRecentProject
} from './recent-projects'

const paths = () => $recentProjects.get().map(entry => entry.path)

describe('recent projects', () => {
  beforeEach(() => {
    $recentProjects.set([])
    $missingProjectPaths.set([])
  })

  it('orders most-recently-opened first', () => {
    recordRecentProject('/a', 1)
    recordRecentProject('/b', 2)
    recordRecentProject('/c', 3)

    expect(paths()).toEqual(['/c', '/b', '/a'])
  })

  it('re-opening moves an entry to the front instead of duplicating it', () => {
    recordRecentProject('/a', 1)
    recordRecentProject('/b', 2)
    recordRecentProject('/a', 3)

    expect(paths()).toEqual(['/a', '/b'])
  })

  it('dedupes paths that differ only in spelling', () => {
    recordRecentProject('/Users/me/proj', 1)
    recordRecentProject('/Users/me/proj/', 2)
    recordRecentProject('/Users/me//proj', 3)
    recordRecentProject('/Users/me/./proj', 4)

    expect(paths()).toEqual(['/Users/me/proj'])
  })

  it('never grows past the cap, evicting the oldest', () => {
    for (let i = 0; i < MAX_RECENT_PROJECTS + 5; i += 1) {
      recordRecentProject(`/p${i}`, i + 1)
    }

    const stored = paths()

    expect(stored).toHaveLength(MAX_RECENT_PROJECTS)
    // Newest survives, oldest evicted.
    expect(stored[0]).toBe(`/p${MAX_RECENT_PROJECTS + 4}`)
    expect(stored).not.toContain('/p0')
  })

  it('ignores unusable paths rather than storing blanks', () => {
    recordRecentProject('')
    recordRecentProject('   ')

    expect(paths()).toEqual([])
  })

  it('forgets a single entry by any spelling, leaving the rest ordered', () => {
    recordRecentProject('/a', 1)
    recordRecentProject('/b', 2)
    forgetRecentProject('/a/')

    expect(paths()).toEqual(['/b'])
  })

  it('clears the whole list', () => {
    recordRecentProject('/a', 1)
    clearRecentProjects()

    expect(paths()).toEqual([])
  })
})

describe('missing project marking', () => {
  beforeEach(() => {
    $recentProjects.set([])
    $missingProjectPaths.set([])
  })

  it('marks and unmarks a path, matching across spellings', () => {
    markProjectMissing('/gone', true)
    expect(isProjectMissing('/gone/')).toBe(true)

    markProjectMissing('/gone/', false)
    expect(isProjectMissing('/gone')).toBe(false)
  })

  it('does not duplicate a repeated mark', () => {
    markProjectMissing('/gone', true)
    markProjectMissing('/gone', true)

    expect($missingProjectPaths.get()).toEqual(['/gone'])
  })

  it('folds availability into the rendered rows without reordering them', () => {
    recordRecentProject('/a', 1)
    recordRecentProject('/b', 2)
    markProjectMissing('/a', true)

    expect($recentProjectRows.get()).toEqual([
      { missing: false, openedAt: 2, path: '/b' },
      { missing: true, openedAt: 1, path: '/a' }
    ])
  })
})
