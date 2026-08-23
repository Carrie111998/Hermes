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

    expect(resolveProjectsGrouping([project('home', true), project('a'), project('b'), project('c')])).toEqual({
      contribution: expect.any(Object),
      groups: [
        { collapsed: false, id: 'one', label: 'One', projects: [project('a')] },
        { collapsed: true, id: 'two', label: 'Two', projects: [project('b')] }
      ],
      home: project('home', true),
      snapshot: expect.any(Object),
      ungrouped: [project('c')]
    })
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
})
