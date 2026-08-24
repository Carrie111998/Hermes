import { describe, expect, it, vi } from 'vitest'

import {
  buildProjectGroupRenamePlan,
  deleteProjectGroup,
  ProjectGroupDeleteCompensationError,
  ProjectGroupDeleteValidationError
} from './project-group-delete'
import type { ProjectsGroupingContribution } from './projects-presentation'

const group = { id: 'group-1', label: '  CUE++  ', projectIds: ['p_alpha', 'p_prefixed'] }

const projects = [
  { id: 'p_alpha', name: '  Alpha  ' },
  { id: 'p_prefixed', name: 'cue++ · Already prefixed' },
  { id: 'p_other', name: 'Other' }
]

function contribution(overrides: Partial<ProjectsGroupingContribution> = {}): ProjectsGroupingContribution {
  return {
    deleteGroup: vi.fn().mockResolvedValue(undefined),
    getSnapshot: () => ({ groups: [group] }),
    subscribe: () => () => undefined,
    ...overrides
  }
}

describe('Project group deletion saga', () => {
  it('builds exact trimmed names without duplicating an existing case-insensitive prefix', () => {
    expect(buildProjectGroupRenamePlan(group, projects)).toEqual([
      { expectedName: '  Alpha  ', id: 'p_alpha', newName: 'CUE++ · Alpha' },
      {
        expectedName: 'cue++ · Already prefixed',
        id: 'p_prefixed',
        newName: 'cue++ · Already prefixed'
      }
    ])
  })

  it('deletes an unchecked group with only the provider CAS request', async () => {
    const provider = contribution()

    await deleteProjectGroup({
      contribution: provider,
      group,
      operationId: 'operation-1',
      prependGroupName: false,
      projects
    })

    expect(provider.deleteGroup).toHaveBeenCalledWith({
      expectedProjectIds: ['p_alpha', 'p_prefixed'],
      groupId: 'group-1',
      operationId: 'operation-1'
    })
  })

  it('aborts on a stale member snapshot before either authority mutates', async () => {
    const provider = contribution({
      getSnapshot: () => ({ groups: [{ ...group, projectIds: ['p_alpha'] }] })
    })

    const renameMany = vi.fn()

    await expect(
      deleteProjectGroup({
        contribution: provider,
        group,
        operationId: 'operation-1',
        prependGroupName: true,
        projects,
        reconcile: vi.fn(),
        renameMany
      })
    ).rejects.toBeInstanceOf(ProjectGroupDeleteValidationError)

    expect(renameMany).not.toHaveBeenCalled()
    expect(provider.deleteGroup).not.toHaveBeenCalled()
  })

  it('rolls every Project name back with CAS when provider deletion fails', async () => {
    const providerFailure = new Error('provider unavailable')
    const provider = contribution({ deleteGroup: vi.fn().mockRejectedValue(providerFailure) })
    const renameMany = vi.fn().mockResolvedValue(projects)

    await expect(
      deleteProjectGroup({
        contribution: provider,
        group,
        operationId: 'operation-1',
        prependGroupName: true,
        projects,
        reconcile: vi.fn(),
        renameMany
      })
    ).rejects.toBe(providerFailure)

    expect(renameMany).toHaveBeenNthCalledWith(1, [
      { expectedName: '  Alpha  ', id: 'p_alpha', newName: 'CUE++ · Alpha' },
      {
        expectedName: 'cue++ · Already prefixed',
        id: 'p_prefixed',
        newName: 'cue++ · Already prefixed'
      }
    ])
    expect(renameMany).toHaveBeenNthCalledWith(2, [
      { expectedName: 'CUE++ · Alpha', id: 'p_alpha', newName: '  Alpha  ' },
      {
        expectedName: 'cue++ · Already prefixed',
        id: 'p_prefixed',
        newName: 'cue++ · Already prefixed'
      }
    ])
  })

  it('reports affected Projects and forces reconciliation when compensation also fails', async () => {
    const providerFailure = new Error('provider unavailable')
    const rollbackFailure = new Error('rollback CAS failed')
    const provider = contribution({ deleteGroup: vi.fn().mockRejectedValue(providerFailure) })
    const renameMany = vi.fn().mockResolvedValueOnce(projects).mockRejectedValueOnce(rollbackFailure)
    const reconcile = vi.fn().mockResolvedValue(undefined)

    const error = await deleteProjectGroup({
      contribution: provider,
      group,
      operationId: 'operation-1',
      prependGroupName: true,
      projects,
      reconcile,
      renameMany
    }).catch(reason => reason)

    expect(error).toBeInstanceOf(ProjectGroupDeleteCompensationError)
    expect(error).toMatchObject({
      affectedProjects: [
        { id: 'p_alpha', name: 'CUE++ · Alpha' },
        { id: 'p_prefixed', name: 'cue++ · Already prefixed' }
      ],
      providerError: providerFailure,
      rollbackError: rollbackFailure
    })
    expect(reconcile).toHaveBeenCalledOnce()
  })

  it('rejects collisions with an ungrouped Project before writing', () => {
    expect(() =>
      buildProjectGroupRenamePlan({ id: 'group-1', label: 'CUE++', projectIds: ['p_alpha'] }, [
        { id: 'p_alpha', name: 'Alpha' },
        { id: 'p_other', name: 'cue++ · alpha' }
      ])
    ).toThrow(ProjectGroupDeleteValidationError)
  })
})
