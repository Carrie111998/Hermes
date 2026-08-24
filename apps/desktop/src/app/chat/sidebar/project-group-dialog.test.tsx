import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { registry } from '@/contrib/registry'

import { ProjectGroupDialog, normalizeProjectGroupName } from './project-group-dialog'
import {
  PROJECTS_GROUPING_AREA,
  type ProjectsGroupingContribution,
  useActiveProjectsGrouping
} from './projects-presentation'

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      common: { cancel: 'Cancel' },
      sidebar: {
        projects: {
          create: 'Create',
          createGroupTitle: 'New project group',
          groupCreateFailed: 'Could not create project group',
          groupNameDuplicate: 'Duplicate group',
          groupNameInvalid: 'Invalid group',
          groupNamePlaceholder: 'Group name'
        }
      }
    }
  })
}))

vi.mock('@/store/notifications', () => ({ notifyError: vi.fn() }))

const disposers: Array<() => void> = []

afterEach(() => {
  cleanup()
  while (disposers.length) disposers.pop()?.()
})

const snapshot = { groups: [{ id: 'cue', label: 'CUE++', projectIds: [] }] }

describe('ProjectGroupDialog', () => {
  it('collapses whitespace and submits the normalized valid name', async () => {
    const createGroup = vi.fn().mockResolvedValue(undefined)
    const onOpenChange = vi.fn()
    const contribution: ProjectsGroupingContribution = {
      createGroup,
      getSnapshot: () => snapshot,
      subscribe: () => () => undefined
    }
    render(<ProjectGroupDialog contribution={contribution} onOpenChange={onOpenChange} open snapshot={snapshot} />)

    fireEvent.change(screen.getByPlaceholderText('Group name'), { target: { value: '  RGC   Labs  ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => expect(createGroup).toHaveBeenCalledWith('RGC Labs'))
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('rejects blank, duplicate and oversized names before mutation', () => {
    const createGroup = vi.fn()
    const contribution: ProjectsGroupingContribution = {
      createGroup,
      getSnapshot: () => snapshot,
      subscribe: () => () => undefined
    }
    render(<ProjectGroupDialog contribution={contribution} onOpenChange={vi.fn()} open snapshot={snapshot} />)

    expect((screen.getByRole('button', { name: 'Create' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(screen.getByPlaceholderText('Group name'), { target: { value: ' cue++ ' } })
    expect(screen.getByText('Duplicate group')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Create' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(screen.getByPlaceholderText('Group name'), { target: { value: 'x'.repeat(101) } })
    expect(screen.getByText('Invalid group')).toBeTruthy()
    expect(createGroup).not.toHaveBeenCalled()
  })

  it('validates names against the same first-valid duplicate-id snapshot as the sidebar and menu', async () => {
    const createGroup = vi.fn().mockResolvedValue(undefined)
    const contribution: ProjectsGroupingContribution = {
      createGroup,
      getSnapshot: () => ({
        groups: [
          { id: 'duplicate', label: 'First group', projectIds: [] },
          { id: 'duplicate', label: 'Discarded group', projectIds: [] }
        ]
      }),
      subscribe: () => () => undefined
    }
    disposers.push(registry.register({ area: PROJECTS_GROUPING_AREA, data: contribution, id: 'groups' }))

    const ConnectedDialog = () => {
      const grouping = useActiveProjectsGrouping()

      return grouping ? (
        <ProjectGroupDialog
          contribution={grouping.contribution}
          onOpenChange={vi.fn()}
          open
          snapshot={grouping.snapshot}
        />
      ) : null
    }

    render(<ConnectedDialog />)
    fireEvent.change(screen.getByPlaceholderText('Group name'), { target: { value: 'Discarded group' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => expect(createGroup).toHaveBeenCalledWith('Discarded group'))
  })
})

describe('normalizeProjectGroupName', () => {
  it('collapses surrounding and internal whitespace', () => {
    expect(normalizeProjectGroupName('  A\n  B  ')).toBe('A B')
  })
})
