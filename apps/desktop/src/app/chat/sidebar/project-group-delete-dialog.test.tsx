import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

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
  refreshProjectTree: vi.fn().mockResolvedValue(undefined),
  refreshProjects: vi.fn().mockResolvedValue(undefined),
  renameProjectsMany: vi.fn().mockResolvedValue([])
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
})

describe('ProjectGroupDeleteDialog', () => {
  it('uses a compact confirmation for an empty group and sends the provider CAS request', async () => {
    const group = { id: 'empty', label: 'Empty', projectIds: [] }
    const { contribution, deleteGroup } = provider(group, vi.fn().mockResolvedValue(undefined))

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
})
