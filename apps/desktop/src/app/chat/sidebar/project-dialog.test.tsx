import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import type * as Nanostores from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ProjectDialog } from './project-dialog'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      common: { cancel: 'Cancel', save: 'Save' },
      sidebar: {
        projects: {
          addFolder: 'Add folder',
          addFolderFailed: (count: number) => `Could not add ${count} folder${count === 1 ? '' : 's'}`,
          addFolderTitle: 'Add folder',
          create: 'Create',
          createDesc: 'Create a new project',
          createFailed: 'Failed to create project',
          createTitle: 'New project',
          foldersLabel: 'Folders',
          ideaGenerate: 'Generate',
          ideaGenerating: 'Generating…',
          ideaLabel: 'Idea',
          ideaPlaceholder: 'What are you building?',
          ideaShuffle: 'Shuffle ideas',
          namePlaceholder: 'Project name',
          noFolders: 'No folders yet',
          primaryBadge: 'Primary',
          removeFolder: 'Remove folder',
          renameTitle: 'Rename project'
        }
      }
    }
  })
}))

// $projectDialog is a real nanostore atom in the app; recreate it here so
// useStore behaves identically without pulling in the rest of the projects
// store (backend calls, project list, etc.) which is irrelevant to the Tip fix.
// vi.mock factories are hoisted above the rest of the file, so the atom must
// be created inside vi.hoisted to exist by the time the factory runs.
const { $projectDialog } = vi.hoisted(() => {
  const { atom } = require('nanostores') as typeof Nanostores

  return {
    $projectDialog: atom<{ mode: 'create' | 'rename' | 'add-folder'; name?: string; projectId?: string } | null>({
      mode: 'create'
    })
  }
})

const { mockAddProjectFolder, mockPickProjectFolders, mockNotifyError } = vi.hoisted(() => ({
  mockAddProjectFolder: vi.fn(),
  mockPickProjectFolders: vi.fn(),
  mockNotifyError: vi.fn()
}))

vi.mock('@/store/projects', () => ({
  $projectDialog,
  addProjectFolder: mockAddProjectFolder,
  closeProjectDialog: vi.fn(),
  createProject: vi.fn(),
  generateProjectIdea: vi.fn(),
  pickProjectFolder: vi.fn(async () => '/Users/test/my-folder'),
  pickProjectFolders: mockPickProjectFolders,
  renameProject: vi.fn()
}))

vi.mock('@/store/notifications', () => ({
  notifyError: mockNotifyError
}))

vi.mock('@/lib/project-idea-templates', () => ({
  randomIdeaTemplates: () => [{ emoji: '🚀', idea: 'A rocket tracker', label: 'Rocket tracker' }]
}))

const tipTrigger = (el: HTMLElement) => el.closest('[data-slot="tooltip-trigger"]')

describe('ProjectDialog', () => {
  it('wraps the "shuffle idea" button in a Tip', () => {
    render(<ProjectDialog />)

    const button = screen.getByRole('button', { name: 'Shuffle ideas' })
    expect(tipTrigger(button)).toBeTruthy()
  })

  it('wraps the "remove folder" button in a Tip once a folder is added', async () => {
    render(<ProjectDialog />)

    fireEvent.click(screen.getByRole('button', { name: 'Add folder' }))

    const button = await screen.findByRole('button', { name: 'Remove folder' })
    expect(tipTrigger(button)).toBeTruthy()
  })
})

describe('ProjectDialog add-folder mode — multi-select loop', () => {
  beforeEach(() => {
    $projectDialog.set({ mode: 'add-folder', projectId: 'p_test' })
  })

  afterEach(() => {
    $projectDialog.set({ mode: 'create' })
  })

  it('calls addProjectFolder once per picked directory', async () => {
    mockPickProjectFolders.mockResolvedValue(['/work/a', '/work/b', '/work/c'])
    mockAddProjectFolder.mockResolvedValue(undefined)

    render(<ProjectDialog />)
    fireEvent.click(screen.getByRole('button', { name: 'Add folder' }))

    // Wait for async ops to settle
    await vi.waitFor(() => {
      expect(mockAddProjectFolder).toHaveBeenCalledTimes(3)
    })

    expect(mockAddProjectFolder).toHaveBeenCalledWith('p_test', '/work/a')
    expect(mockAddProjectFolder).toHaveBeenCalledWith('p_test', '/work/b')
    expect(mockAddProjectFolder).toHaveBeenCalledWith('p_test', '/work/c')
    expect(mockNotifyError).not.toHaveBeenCalled()
  })

  it('continues iterating after a per-folder failure and surfaces errors for failed folders', async () => {
    mockPickProjectFolders.mockResolvedValue(['/work/ok', '/work/fail', '/work/also-ok'])
    mockAddProjectFolder
      .mockResolvedValueOnce(undefined)         // /work/ok succeeds
      .mockRejectedValueOnce(new Error('permission denied'))  // /work/fail fails
      .mockResolvedValueOnce(undefined)         // /work/also-ok succeeds

    render(<ProjectDialog />)
    fireEvent.click(screen.getByRole('button', { name: 'Add folder' }))

    // All three folders are attempted despite the middle failure
    await vi.waitFor(() => {
      expect(mockAddProjectFolder).toHaveBeenCalledTimes(3)
    })

    expect(mockAddProjectFolder).toHaveBeenCalledWith('p_test', '/work/ok')
    expect(mockAddProjectFolder).toHaveBeenCalledWith('p_test', '/work/fail')
    expect(mockAddProjectFolder).toHaveBeenCalledWith('p_test', '/work/also-ok')

    // The partial failure is surfaced to the user
    expect(mockNotifyError).toHaveBeenCalledOnce()
  })

  it('does not call addProjectFolder when the picker is dismissed (empty selection)', async () => {
    mockPickProjectFolders.mockResolvedValue([])

    render(<ProjectDialog />)
    fireEvent.click(screen.getByRole('button', { name: 'Add folder' }))

    await vi.waitFor(() => {
      // Give async path time to settle
      expect(mockPickProjectFolders).toHaveBeenCalled()
    })

    expect(mockAddProjectFolder).not.toHaveBeenCalled()
    expect(mockNotifyError).not.toHaveBeenCalled()
  })
})
