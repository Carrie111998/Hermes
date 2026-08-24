import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { registry } from '@/contrib/registry'

import {
  type ProjectGroupDescriptor,
  PROJECTS_GROUPING_AREA,
  resolveProjectsGrouping,
  type ProjectsGroupingContribution,
  useActiveProjectsGrouping,
  useProjectsGrouping
} from './projects-presentation'
import type { SidebarProjectTree } from './projects'

const project = (id: string, isNoProject = false): SidebarProjectTree =>
  ({
    id,
    isNoProject,
    label: id,
    path: `/work/${id}`,
    repos: [],
    sessionCount: 0,
    previewSessions: []
  }) as SidebarProjectTree

const disposers: Array<() => void> = []
afterEach(() => {
  while (disposers.length) disposers.pop()?.()
})

function provider(groups: readonly ProjectGroupDescriptor[]): ProjectsGroupingContribution {
  const snapshot = { groups }
  return { getSnapshot: () => snapshot, subscribe: () => () => undefined }
}

function register(data: unknown, order = 0) {
  disposers.push(registry.register({ area: PROJECTS_GROUPING_AREA, data, id: `provider-${order}`, order }))
}

describe('Projects grouping contribution', () => {
  it('returns null without a provider so the built-in flat list is unchanged', () => {
    expect(resolveProjectsGrouping([project('a')])).toBeNull()
  })

  it('skips a malformed higher-priority contribution', () => {
    register({ groups: [] }, 0)
    register(provider([{ id: 'valid', label: 'Valid', projectIds: ['a'] }]), 10)

    expect(resolveProjectsGrouping([project('a')])?.groups[0].id).toBe('valid')
  })

  it.each([
    { id: 42, label: 'Broken', projectIds: ['a'] },
    { id: '   ', label: 'Broken', projectIds: ['a'] },
    { id: 'broken', label: null, projectIds: ['a'] },
    { id: 'broken', label: '   ', projectIds: ['a'] },
    { id: 'broken', label: 'Broken', projectIds: 'a' },
    { id: 'broken', label: 'Broken', projectIds: ['a', 42] }
  ])('rejects a provider with malformed group descriptor %# and sanitizes the fallback', malformed => {
    register({ getSnapshot: () => ({ groups: [malformed] }), subscribe: () => () => undefined }, 0)
    register(provider([{ id: ' valid ', label: ' Valid ', projectIds: [' a '] }]), 10)

    const resolved = resolveProjectsGrouping([project('a')])
    const { result } = renderHook(() => useActiveProjectsGrouping())

    expect(resolved?.groups).toEqual([{ collapsed: false, id: 'valid', label: 'Valid', projects: [project('a')] }])
    expect(result.current?.snapshot.groups).toEqual([{ id: 'valid', label: 'Valid', projectIds: ['a'] }])
  })

  it('keeps Home fixed and sends stale, duplicate and omitted ids to core Ungrouped', () => {
    register(
      provider([
        { id: ' one ', label: ' One ', projectIds: ['a', 'missing', 'a'] },
        { id: 'two', label: 'Two', projectIds: ['a', 'b'], collapsed: true },
        { id: 'two', label: 'Duplicate', projectIds: ['c'] }
      ])
    )

    const projects = [project('home', true), project('a'), project('b'), project('c')]
    const resolved = resolveProjectsGrouping(projects)
    const { result } = renderHook(() => useActiveProjectsGrouping())

    expect(resolved).toEqual({
      contribution: expect.any(Object),
      groups: [
        { collapsed: false, id: 'one', label: 'One', projects: [project('a')] },
        { collapsed: true, id: 'two', label: 'Two', projects: [project('b')] }
      ],
      home: project('home', true),
      snapshot: expect.any(Object),
      ungrouped: [project('c')]
    })
    expect(result.current?.snapshot.groups).toEqual([
      { id: 'one', label: 'One', projectIds: ['a', 'missing', 'a'] },
      { collapsed: true, id: 'two', label: 'Two', projectIds: ['a', 'b'] }
    ])
  })

  it('falls through when the highest-priority valid provider throws while subscribing', () => {
    register(
      {
        getSnapshot: () => ({ groups: [{ id: 'broken', label: 'Broken', projectIds: ['a'] }] }),
        subscribe: () => {
          throw new Error('subscription failed')
        }
      },
      0
    )
    register(provider([{ id: 'fallback', label: 'Fallback', projectIds: ['b'] }]), 10)

    const { result } = renderHook(() => useProjectsGrouping([project('a'), project('b')]))

    expect(result.current?.groups).toEqual([
      { collapsed: false, id: 'fallback', label: 'Fallback', projects: [project('b')] }
    ])
  })

  it('shares call-dependent subscription failover across concurrent consumers', () => {
    let subscribeCalls = 0
    const primary = provider([{ id: 'primary', label: 'Primary', projectIds: ['a'] }])
    primary.assignProject = vi.fn()

    primary.subscribe = () => {
      subscribeCalls += 1

      if (subscribeCalls === 1) {
        throw new Error('first subscription failed')
      }

      return () => undefined
    }

    const fallback = provider([{ id: 'fallback', label: 'Fallback', projectIds: ['b'] }])
    fallback.assignProject = vi.fn()
    register(primary, 0)
    register(fallback, 10)

    const { result } = renderHook(() => ({
      dialog: useActiveProjectsGrouping(),
      menu: useActiveProjectsGrouping(),
      sessions: useProjectsGrouping([project('a'), project('b')]),
      sidebar: useProjectsGrouping([project('a'), project('b')])
    }))

    expect(result.current.dialog?.contribution).toBe(fallback)
    expect(result.current.menu?.contribution).toBe(fallback)
    expect(result.current.sessions?.contribution).toBe(fallback)
    expect(result.current.sidebar?.contribution).toBe(fallback)
    expect(result.current.dialog?.snapshot.groups[0].id).toBe('fallback')
    expect(result.current.menu?.snapshot).toBe(result.current.dialog?.snapshot)
    expect(result.current.sessions?.snapshot).toBe(result.current.dialog?.snapshot)
    expect(result.current.sidebar?.snapshot).toBe(result.current.dialog?.snapshot)
    expect(result.current.sessions?.groups[0].id).toBe('fallback')
    expect(result.current.sidebar?.groups[0].id).toBe('fallback')
    expect(subscribeCalls).toBe(1)

    const recovered = provider([{ id: 'recovered', label: 'Recovered', projectIds: ['a'] }])
    act(() => register(recovered, -10))

    expect(result.current.dialog?.contribution).toBe(recovered)
    expect(result.current.menu?.contribution).toBe(recovered)
    expect(result.current.sessions?.contribution).toBe(recovered)
    expect(result.current.sidebar?.contribution).toBe(recovered)
    expect(result.current.menu?.snapshot).toBe(result.current.dialog?.snapshot)
    expect(result.current.sessions?.snapshot).toBe(result.current.dialog?.snapshot)
    expect(result.current.sidebar?.snapshot).toBe(result.current.dialog?.snapshot)
  })

  it('settles on a stable empty state when every valid provider throws while subscribing', () => {
    register({
      getSnapshot: () => ({ groups: [{ id: 'broken', label: 'Broken', projectIds: ['a'] }] }),
      subscribe: () => {
        throw new Error('subscription failed')
      }
    })

    const { rerender, result } = renderHook(() => useActiveProjectsGrouping())

    expect(result.current).toBeNull()
    rerender()
    expect(result.current).toBeNull()
  })

  it('uses deterministic contribution ordering and reads one stable snapshot', () => {
    const getSnapshot = vi.fn(() => ({ groups: [{ id: 'early', label: 'Early', projectIds: ['a'] }] }))
    register(provider([{ id: 'late', label: 'Late', projectIds: ['b'] }]), 20)
    register({ getSnapshot, subscribe: () => () => undefined }, 10)

    expect(resolveProjectsGrouping([project('a'), project('b')])?.groups[0].id).toBe('early')
    expect(getSnapshot).toHaveBeenCalledTimes(1)
  })

  it('keeps class-provider receivers in both reactive hooks', () => {
    class ClassProvider implements ProjectsGroupingContribution {
      private listeners = new Set<() => void>()
      private snapshot = { groups: [{ id: 'one', label: 'One', projectIds: ['a'] }] }

      getSnapshot() {
        return this.snapshot
      }

      subscribe(listener: () => void) {
        this.listeners.add(listener)
        return () => this.listeners.delete(listener)
      }

      update() {
        this.snapshot = { groups: [{ id: 'two', label: 'Two', projectIds: ['b'] }] }
        this.listeners.forEach(listener => listener())
      }
    }

    const contribution = new ClassProvider()
    register(contribution)

    const { result } = renderHook(() => ({
      active: useActiveProjectsGrouping(),
      presented: useProjectsGrouping([project('a'), project('b')])
    }))

    expect(result.current.active?.snapshot.groups[0].id).toBe('one')
    expect(result.current.presented?.groups[0].projects).toEqual([project('a')])

    act(() => contribution.update())

    expect(result.current.active?.snapshot.groups[0].id).toBe('two')
    expect(result.current.presented?.groups[0].projects).toEqual([project('b')])
  })

  it('stabilizes fresh equivalent snapshots returned by a defensive provider adapter', () => {
    let groupId = 'one'
    let listener: (() => void) | undefined
    const contribution: ProjectsGroupingContribution = {
      getSnapshot: () => ({ groups: [{ id: groupId, label: groupId, projectIds: ['a'] }] }),
      subscribe: onChange => {
        listener = onChange
        return () => undefined
      }
    }
    register(contribution)

    const { result } = renderHook(() => useActiveProjectsGrouping())

    expect(result.current?.snapshot.groups).toEqual([{ id: 'one', label: 'one', projectIds: ['a'] }])

    act(() => {
      groupId = 'two'
      listener?.()
    })

    expect(result.current?.snapshot.groups).toEqual([{ id: 'two', label: 'two', projectIds: ['a'] }])
  })

  it.each([
    ['opaque-token', 'opaque-token'],
    [0, 0]
  ])('retains a valid opaque snapshot revision %#', (revision, expected) => {
    register({
      getSnapshot: () => ({ groups: [], revision }),
      subscribe: () => () => undefined
    })

    expect(resolveProjectsGrouping([])?.snapshot).toEqual({ groups: [], revision: expected })
  })

  it.each([true, null, {}, [], Number.NaN, Number.POSITIVE_INFINITY])(
    'ignores a malformed snapshot revision %# without rejecting the provider',
    revision => {
      register({
        getSnapshot: () => ({ groups: [{ id: 'one', label: 'One', projectIds: ['a'] }], revision }),
        subscribe: () => () => undefined
      })

      expect(resolveProjectsGrouping([project('a')])?.snapshot).toEqual({
        groups: [{ id: 'one', label: 'One', projectIds: ['a'] }]
      })
    }
  )

  it('publishes a new cache identity when only the provider revision changes', () => {
    let revision: number | string = 1
    let listener: (() => void) | undefined
    const contribution: ProjectsGroupingContribution = {
      getSnapshot: () => ({ groups: [{ id: 'one', label: 'One', projectIds: ['a'] }], revision }),
      subscribe: onChange => {
        listener = onChange
        return () => undefined
      }
    }
    register(contribution)

    const { result } = renderHook(() => useActiveProjectsGrouping())
    const initial = result.current?.snapshot

    act(() => {
      revision = 'capability-added'
      listener?.()
    })

    expect(result.current?.snapshot).not.toBe(initial)
    expect(result.current?.snapshot).toEqual({
      groups: [{ id: 'one', label: 'One', projectIds: ['a'] }],
      revision: 'capability-added'
    })
  })
})
