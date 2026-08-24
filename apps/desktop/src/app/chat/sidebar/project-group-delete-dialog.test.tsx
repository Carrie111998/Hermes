import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { withActiveProjectsContext } from '@/store/projects'

import { ProjectGroupDeleteDialog } from './project-group-delete-dialog'
import type { ProjectsGroupingContribution } from './projects-presentation'

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      common: {
        cancel: 'Cancel',
        close: 'Close',
        confirm: 'Confirm',
        done: 'Done',
        loading: 'Loading…'
      },
      errors: { genericFailure: 'Something went wrong' },
      sidebar: {
        projects: {
          deleteGroup: 'Delete group',
          deleteGroupCollision: 'A resulting Project name already exists.',
          deleteGroupCompensationFailed: (names: string) => `Rollback failed for: ${names}`,
          deleteGroupEmptyDescription: 'The empty group will be removed.',
          deleteGroupFailed: 'Could not delete project group',
          deleteGroupMoveDescription: 'Every Project in this group will be moved to Ungrouped.',
          deleteGroupPending: 'Deleting…',
          deleteGroupPrepend: 'Prepend old group name to Project names',
          deleteGroupPreviewAfter: 'Resulting names',
          deleteGroupPreviewBefore: 'Current group',
          deleteGroupPreviewLabel: 'Project rename preview',
          deleteGroupStale: 'This group changed. Review it again.',
          deleteGroupTitle: (name: string) => `Delete group “${name}”?`
        }
      }
    }
  })
}))

vi.mock('@/store/projects', () => ({
  withActiveProjectsContext: vi.fn(
    (
      operation: (operations: { reconcile: () => Promise<void>; renameMany: () => Promise<never[]> }) => Promise<void>
    ) =>
      operation({
        reconcile: vi.fn().mockResolvedValue(undefined),
        renameMany: vi.fn().mockResolvedValue([])
      })
  )
}))

const projects = [
  { id: 'p_alpha', name: 'Alpha' },
  { id: 'p_beta', name: 'Beta' },
  { id: 'p_other', name: 'Other' }
]

function provider(group: { id: string; label: string; projectIds: string[] }, deleteGroup = vi.fn()) {
  return {
    contribution: {
      deleteGroup,
      getSnapshot: () => ({ groups: [group] }),
      subscribe: () => () => undefined
    } satisfies ProjectsGroupingContribution,
    deleteGroup
  }
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.mocked(withActiveProjectsContext).mockImplementation(operation =>
    operation({
      reconcile: vi.fn().mockResolvedValue(undefined),
      renameMany: vi.fn().mockResolvedValue([])
    })
  )
})

describe('ProjectGroupDeleteDialog', () => {
  it('uses a compact confirmation for an empty group and sends the provider CAS request', async () => {
    const group = { id: 'empty', label: 'Empty', projectIds: [] }
    const { contribution, deleteGroup } = provider(group, vi.fn().mockResolvedValue(undefined))
    vi.mocked(withActiveProjectsContext).mockRejectedValue(new Error('Projects unavailable in All Profiles'))

    render(
      <ProjectGroupDeleteDialog
        contribution={contribution}
        group={group}
        onOpenChange={vi.fn()}
        open
        projects={projects}
      />
    )

    expect(screen.getByRole('heading', { name: 'Delete group “Empty”?' })).toBeTruthy()
    expect(screen.getByText('The empty group will be removed.')).toBeTruthy()
    expect(screen.queryByRole('checkbox')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Delete group' }))

    await waitFor(() => expect(deleteGroup).toHaveBeenCalledOnce())
    expect(deleteGroup).toHaveBeenCalledWith({
      expectedProjectIds: [],
      groupId: 'empty',
      operationId: expect.any(String)
    })
    expect(withActiveProjectsContext).not.toHaveBeenCalled()
  })

  it('deletes a nonempty unchecked group in All Profiles without capturing Projects context', async () => {
    const group = { id: 'work', label: 'Work', projectIds: ['p_alpha'] }
    const { contribution, deleteGroup } = provider(group, vi.fn().mockResolvedValue(undefined))
    vi.mocked(withActiveProjectsContext).mockRejectedValue(new Error('Projects unavailable in All Profiles'))
    const onOpenChange = vi.fn()

    render(
      <ProjectGroupDeleteDialog
        contribution={contribution}
        group={group}
        onOpenChange={onOpenChange}
        open
        projects={projects}
      />
    )

    expect(screen.getByRole('checkbox').getAttribute('data-state')).toBe('unchecked')
    fireEvent.click(screen.getByRole('button', { name: 'Delete group' }))

    await waitFor(() => expect(deleteGroup).toHaveBeenCalledOnce())
    expect(withActiveProjectsContext).not.toHaveBeenCalled()
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('starts unchecked with no preview, then renders the exact tree-to-flat preview', async () => {
    const group = { id: 'work', label: ' CUE++ ', projectIds: ['p_alpha', 'p_beta'] }
    const { contribution } = provider(group)

    render(
      <ProjectGroupDeleteDialog
        contribution={contribution}
        group={group}
        onOpenChange={vi.fn()}
        open
        projects={projects}
      />
    )

    expect(screen.getByText('Every Project in this group will be moved to Ungrouped.')).toBeTruthy()
    const checkbox = screen.getByRole('checkbox', { name: 'Prepend old group name to Project names' })
    expect(checkbox.getAttribute('data-state')).toBe('unchecked')
    expect(screen.queryByRole('group', { name: 'Project rename preview' })).toBeNull()

    fireEvent.click(checkbox)

    const preview = await screen.findByRole('group', { name: 'Project rename preview' })
    const before = within(preview).getByRole('tree', { name: 'Current group' })
    const after = within(preview).getByRole('list', { name: 'Resulting names' })
    expect(
      within(before)
        .getAllByRole('treeitem')
        .map(item => item.textContent)
    ).toEqual(['CUE++AlphaBeta', 'Alpha', 'Beta'])
    expect(
      within(after)
        .getAllByRole('listitem')
        .map(item => item.textContent)
    ).toEqual(['CUE++ · Alpha', 'CUE++ · Beta'])
  })

  it('blocks a colliding preview before mutation', async () => {
    const group = { id: 'work', label: 'CUE++', projectIds: ['p_alpha'] }
    const collidingProjects = [...projects, { id: 'collision', name: 'cue++ · alpha' }]
    const { contribution, deleteGroup } = provider(group)

    render(
      <ProjectGroupDeleteDialog
        contribution={contribution}
        group={group}
        onOpenChange={vi.fn()}
        open
        projects={collidingProjects}
      />
    )
    fireEvent.click(screen.getByRole('checkbox'))

    expect((await screen.findByRole('alert')).textContent).toContain('A resulting Project name already exists.')
    expect((screen.getByRole('button', { name: 'Delete group' }) as HTMLButtonElement).disabled).toBe(true)
    expect(deleteGroup).not.toHaveBeenCalled()
  })

  it('focuses the destructive action, supports Enter, and preserves selection on an inline error', async () => {
    const group = { id: 'work', label: 'CUE++', projectIds: ['p_alpha'] }
    let rejectDelete!: (error: Error) => void

    const deletion = new Promise<void>((_resolve, reject) => {
      rejectDelete = reject
    })

    const { contribution, deleteGroup } = provider(
      group,
      vi.fn(() => deletion)
    )

    render(
      <ProjectGroupDeleteDialog
        contribution={contribution}
        group={group}
        onOpenChange={vi.fn()}
        open
        projects={projects}
      />
    )
    const checkbox = screen.getByRole('checkbox')
    fireEvent.click(checkbox)
    const deleteButton = screen.getByRole('button', { name: 'Delete group' })
    await waitFor(() => expect(globalThis.document.activeElement).toBe(deleteButton))

    fireEvent.keyDown(deleteButton, { key: 'Enter' })
    await waitFor(() => expect(deleteGroup).toHaveBeenCalledOnce())
    expect((screen.getByRole('button', { name: 'Deleting…' }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: 'Cancel' }) as HTMLButtonElement).disabled).toBe(true)

    rejectDelete(new Error('provider unavailable'))
    expect((await screen.findByRole('alert')).textContent).toContain('provider unavailable')
    expect(checkbox.getAttribute('data-state')).toBe('checked')
    expect(screen.getByRole('group', { name: 'Project rename preview' })).toBeTruthy()
  })

  it('resets the review and operation identity when the live label or exact member set changes', async () => {
    let liveGroup = { id: 'work', label: 'CUE++', projectIds: ['p_alpha'] }
    const deleteGroup = vi.fn().mockRejectedValueOnce(new Error('retry review')).mockResolvedValue(undefined)

    const contribution = {
      deleteGroup,
      getSnapshot: () => ({ groups: [liveGroup] }),
      subscribe: () => () => undefined
    } satisfies ProjectsGroupingContribution

    const onOpenChange = vi.fn()

    const { rerender } = render(
      <ProjectGroupDeleteDialog
        contribution={contribution}
        group={liveGroup}
        onOpenChange={onOpenChange}
        open
        projects={projects}
      />
    )

    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(screen.getByRole('button', { name: 'Delete group' }))
    expect((await screen.findByRole('alert')).textContent).toContain('retry review')
    const firstOperationId = deleteGroup.mock.calls[0][0].operationId

    liveGroup = { ...liveGroup, label: 'Renamed group' }
    rerender(
      <ProjectGroupDeleteDialog
        contribution={contribution}
        group={liveGroup}
        onOpenChange={onOpenChange}
        open
        projects={projects}
      />
    )

    await waitFor(() => expect(screen.getByRole('checkbox').getAttribute('data-state')).toBe('unchecked'))
    expect(screen.queryByRole('group', { name: 'Project rename preview' })).toBeNull()

    fireEvent.click(screen.getByRole('checkbox'))
    liveGroup = { ...liveGroup, projectIds: ['p_alpha', 'p_beta'] }
    rerender(
      <ProjectGroupDeleteDialog
        contribution={contribution}
        group={liveGroup}
        onOpenChange={onOpenChange}
        open
        projects={projects}
      />
    )

    await waitFor(() => expect(screen.getByRole('checkbox').getAttribute('data-state')).toBe('unchecked'))
    expect(screen.queryByRole('group', { name: 'Project rename preview' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Delete group' }))

    await waitFor(() => expect(deleteGroup).toHaveBeenCalledTimes(2))
    expect(deleteGroup.mock.calls[1][0]).toMatchObject({
      expectedProjectIds: ['p_alpha', 'p_beta'],
      groupId: 'work'
    })
    expect(deleteGroup.mock.calls[1][0].operationId).not.toBe(firstOperationId)
  })
})
