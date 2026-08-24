import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { registry } from '@/contrib/registry'

import { PROJECTS_GROUPING_AREA, type ProjectsGroupingContribution } from '../projects-presentation'
import { ProjectMenu } from './project-menu'
import type { SidebarProjectTree } from './workspace-groups'

const disposers: Array<() => void> = []

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  while (disposers.length) disposers.pop()?.()
})

// jsdom doesn't implement ResizeObserver; Radix's PopoverContent/Arrow use it
// (via @radix-ui/react-use-size) to measure the arrow once the popover is
// actually mounted. The kebab-only test above never opens a Popover, so it
// doesn't need this — only the appearance-popover test below does.
beforeAll(() => {
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
  )
})

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      common: { cancel: 'Cancel', confirm: 'Confirm', done: 'Done', loading: 'Loading…' },
      sidebar: {
        projects: {
          copyPath: 'Copy path',
          deleteConfirm: 'This cannot be undone.',
          menu: 'Actions',
          menuAddFolder: 'Add folder',
          menuAppearance: 'Appearance',
          menuDelete: 'Delete',
          menuRename: 'Rename',
          menuSetActive: 'Set active',
          groupUpdateFailed: 'Could not update Project group.',
          moveToGroup: 'Move to group',
          noColor: 'No color',
          removeFromSidebar: 'Remove from sidebar',
          reveal: 'Reveal in file manager',
          ungrouped: 'Ungrouped'
        }
      }
    }
  })
}))

vi.mock('@/store/layout', () => ({
  $panesFlipped: {
    get: () => false,
    listen: () => () => {},
    subscribe: (fn: (v: boolean) => void) => {
      fn(false)

      return () => {}
    }
  },
  dismissAutoProject: vi.fn()
}))

vi.mock('@/store/projects', () => ({
  copyPath: vi.fn(),
  deleteProject: vi.fn(),
  materializeAutoProject: vi.fn(),
  openProjectAddFolder: vi.fn(),
  openProjectRename: vi.fn(),
  revealPath: vi.fn(),
  setActiveProject: vi.fn(),
  setProjectAppearance: vi.fn().mockResolvedValue(false)
}))

vi.mock('@/store/notifications', () => ({
  notifyError: vi.fn()
}))

const project = {
  color: null,
  icon: null,
  id: 'p1',
  isAuto: false,
  label: 'Test D',
  path: '/repo'
} as unknown as SidebarProjectTree

const projectsStore = await import('@/store/projects')
const materializeAutoProject = vi.mocked(projectsStore.materializeAutoProject)
const setProjectAppearance = vi.mocked(projectsStore.setProjectAppearance)
const notifications = await import('@/store/notifications')
const notifyError = vi.mocked(notifications.notifyError)

const tipTrigger = (el: HTMLElement) => el.closest('[data-slot="tooltip-trigger"]')

const openTriggerMenu = (trigger: HTMLElement) => {
  // Radix's dropdown trigger opens on pointerdown (a synthetic 'click' fireEvent
  // alone won't do it), so fire the full mouse sequence a real click produces —
  // same technique as session-actions-menu.test.tsx (#67500).
  fireEvent.pointerDown(trigger, { button: 0, pointerType: 'mouse' })
  fireEvent.pointerUp(trigger, { button: 0, pointerType: 'mouse' })
  fireEvent.click(trigger)
}

describe('ProjectMenu', () => {
  it('does not wrap the kebab trigger in a Tip', () => {
    render(<ProjectMenu isActive={false} project={project} />)

    const button = screen.getByRole('button', { name: 'Actions' })
    expect(tipTrigger(button)).toBeNull()
  })

  // When anchorRef is absent, PopoverAnchor wraps the dropdown trigger so the
  // appearance popover positions against the kebab. asChild must still reach
  // the real button (no non-forwarding wrappers inside the chain — #67500).
  it('opens the appearance popover through the kebab trigger when anchorRef is absent', async () => {
    render(<ProjectMenu isActive={false} project={project} />)

    const trigger = screen.getByRole('button', { name: 'Actions' })

    openTriggerMenu(trigger)

    const appearanceItem = await screen.findByRole('menuitem', { name: 'Appearance' })

    fireEvent.click(appearanceItem)

    // The color-swatch "No color" clear option only renders once the
    // appearance Popover is actually open — proving the click reached the
    // real button through the full Tip > PopoverAnchor > DropdownMenuTrigger
    // chain rather than getting silently dropped on an intermediate wrapper.
    expect(await screen.findByRole('button', { name: 'No color' })).toBeTruthy()
  }, 15000)

  it('offers only the first valid group when a provider repeats a group id', async () => {
    const contribution: ProjectsGroupingContribution = {
      assignProject: vi.fn(),
      getSnapshot: () => ({
        groups: [
          { id: 'duplicate', label: 'First group', projectIds: [] },
          { id: 'duplicate', label: 'Discarded group', projectIds: [] }
        ]
      }),
      subscribe: () => () => undefined
    }
    disposers.push(registry.register({ area: PROJECTS_GROUPING_AREA, data: contribution, id: 'groups' }))

    render(<ProjectMenu isActive={false} project={project} />)
    openTriggerMenu(screen.getByRole('button', { name: 'Actions' }))
    fireEvent.pointerMove(await screen.findByRole('menuitem', { name: 'Move to group' }), { pointerType: 'mouse' })
    fireEvent.pointerEnter(screen.getByRole('menuitem', { name: 'Move to group' }), { pointerType: 'mouse' })

    expect(await screen.findByRole('menuitem', { name: 'First group' })).toBeTruthy()
    expect(screen.queryByRole('menuitem', { name: 'Discarded group' })).toBeNull()
  }, 15000)

  it('adopts an auto Project before grouping so later appearance edits keep its membership', async () => {
    const autoProject = { ...project, id: '/repo', isAuto: true }
    const adoptedProject = { ...project, id: 'p_stable', isAuto: false }
    let snapshot = { groups: [{ id: 'cue', label: 'CUE++', projectIds: [] as string[] }] }
    const assignProject = vi.fn(async (projectId: string, groupId: string | null) => {
      snapshot = {
        groups: [
          {
            ...snapshot.groups[0],
            projectIds: [
              ...snapshot.groups[0].projectIds.filter(id => id !== projectId),
              ...(groupId === 'cue' ? [projectId] : [])
            ]
          }
        ]
      }
    })
    const contribution: ProjectsGroupingContribution = {
      assignProject,
      getSnapshot: () => snapshot,
      subscribe: () => () => undefined
    }
    disposers.push(registry.register({ area: PROJECTS_GROUPING_AREA, data: contribution, id: 'groups' }))
    materializeAutoProject.mockResolvedValue(adoptedProject as never)

    const view = render(<ProjectMenu isActive={false} project={autoProject} />)
    openTriggerMenu(screen.getByRole('button', { name: 'Actions' }))
    const moveToGroup = await screen.findByRole('menuitem', { name: 'Move to group' })
    fireEvent.pointerMove(moveToGroup, { pointerType: 'mouse' })
    fireEvent.pointerEnter(moveToGroup, { pointerType: 'mouse' })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'CUE++' }))

    await waitFor(() => expect(assignProject).toHaveBeenCalledWith('p_stable', 'cue'))
    expect(materializeAutoProject).toHaveBeenCalledWith(autoProject)

    view.rerender(<ProjectMenu isActive={false} project={adoptedProject} />)
    openTriggerMenu(screen.getByRole('button', { name: 'Actions' }))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Appearance' }))
    fireEvent.click(await screen.findByRole('button', { name: 'No color' }))

    await waitFor(() => expect(setProjectAppearance).toHaveBeenCalledWith(adoptedProject, { color: null }))
    expect(snapshot.groups[0].projectIds).toEqual(['p_stable'])
  }, 15000)

  it('migrates legacy path-id membership to the stable Project even when the chosen group is unchanged', async () => {
    const autoProject = { ...project, id: '/repo', isAuto: true }
    const adoptedProject = { ...project, id: 'p_stable', isAuto: false }
    let snapshot = { groups: [{ id: 'cue', label: 'CUE++', projectIds: ['/repo'] }] }
    const assignProject = vi.fn(async (projectId: string, groupId: string | null) => {
      snapshot = {
        groups: snapshot.groups.map(group => ({
          ...group,
          projectIds: [...group.projectIds.filter(id => id !== projectId), ...(group.id === groupId ? [projectId] : [])]
        }))
      }
    })
    const contribution: ProjectsGroupingContribution = {
      assignProject,
      getSnapshot: () => snapshot,
      subscribe: () => () => undefined
    }
    disposers.push(registry.register({ area: PROJECTS_GROUPING_AREA, data: contribution, id: 'groups' }))
    materializeAutoProject.mockResolvedValue(adoptedProject as never)

    render(<ProjectMenu isActive={false} project={autoProject} />)
    openTriggerMenu(screen.getByRole('button', { name: 'Actions' }))
    fireEvent.pointerMove(await screen.findByRole('menuitem', { name: 'Move to group' }), { pointerType: 'mouse' })
    fireEvent.pointerEnter(screen.getByRole('menuitem', { name: 'Move to group' }), { pointerType: 'mouse' })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'CUE++' }))

    await waitFor(() => expect(assignProject).toHaveBeenCalledTimes(2))
    expect(assignProject.mock.calls).toEqual([
      ['p_stable', 'cue'],
      ['/repo', null]
    ])
    expect(snapshot.groups[0].projectIds).toEqual(['p_stable'])
  }, 15000)

  it('clears stable and legacy path ids when an auto Project is moved to Ungrouped', async () => {
    const autoProject = { ...project, id: '/repo', isAuto: true }
    const adoptedProject = { ...project, id: 'p_stable', isAuto: false }
    const assignProject = vi.fn().mockResolvedValue(undefined)
    const contribution: ProjectsGroupingContribution = {
      assignProject,
      getSnapshot: () => ({ groups: [{ id: 'cue', label: 'CUE++', projectIds: ['/repo'] }] }),
      subscribe: () => () => undefined
    }
    disposers.push(registry.register({ area: PROJECTS_GROUPING_AREA, data: contribution, id: 'groups' }))
    materializeAutoProject.mockResolvedValue(adoptedProject as never)

    render(<ProjectMenu isActive={false} project={autoProject} />)
    openTriggerMenu(screen.getByRole('button', { name: 'Actions' }))
    fireEvent.pointerMove(await screen.findByRole('menuitem', { name: 'Move to group' }), { pointerType: 'mouse' })
    fireEvent.pointerEnter(screen.getByRole('menuitem', { name: 'Move to group' }), { pointerType: 'mouse' })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Ungrouped' }))

    await waitFor(() => expect(assignProject).toHaveBeenCalledTimes(2))
    expect(assignProject.mock.calls).toEqual([
      ['p_stable', null],
      ['/repo', null]
    ])
  }, 15000)

  it('leaves a provider partial migration visible and reports the failure without fabricating rollback state', async () => {
    const autoProject = { ...project, id: '/repo', isAuto: true }
    const adoptedProject = { ...project, id: 'p_stable', isAuto: false }
    let snapshot = { groups: [{ id: 'cue', label: 'CUE++', projectIds: ['/repo'] }] }
    const assignProject = vi.fn(async (projectId: string, groupId: string | null) => {
      if (projectId === '/repo') throw new Error('transient id cleanup failed')
      snapshot = {
        groups: [{ ...snapshot.groups[0], projectIds: groupId === 'cue' ? ['/repo', projectId] : ['/repo'] }]
      }
    })
    const contribution: ProjectsGroupingContribution = {
      assignProject,
      getSnapshot: () => snapshot,
      subscribe: () => () => undefined
    }
    disposers.push(registry.register({ area: PROJECTS_GROUPING_AREA, data: contribution, id: 'groups' }))
    materializeAutoProject.mockResolvedValue(adoptedProject as never)

    render(<ProjectMenu isActive={false} project={autoProject} />)
    openTriggerMenu(screen.getByRole('button', { name: 'Actions' }))
    fireEvent.pointerMove(await screen.findByRole('menuitem', { name: 'Move to group' }), { pointerType: 'mouse' })
    fireEvent.pointerEnter(screen.getByRole('menuitem', { name: 'Move to group' }), { pointerType: 'mouse' })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'CUE++' }))

    await waitFor(() => expect(notifyError).toHaveBeenCalledWith(expect.any(Error), 'Could not update Project group.'))
    expect(snapshot.groups[0].projectIds).toEqual(['/repo', 'p_stable'])
  }, 15000)
})
