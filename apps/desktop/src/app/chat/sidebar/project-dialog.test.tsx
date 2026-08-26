import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import type * as Nanostores from 'nanostores'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ProjectDialog } from './project-dialog'

afterEach(cleanup)

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      common: { cancel: 'Cancel', save: 'Save' },
      settings: {
        connections: {
          kindCloud: 'Cloud',
          kindLocal: 'Local',
          kindRemote: 'Remote',
          kindSsh: 'SSH'
        }
      },
      sidebar: {
        projects: {
          addFolder: 'Add folder',
          create: 'Create',
          createDesc: 'Create a new project',
          createFailed: 'Failed to create project',
          createTitle: 'New project',
          foldersLabel: 'Folders',
          gatewayLabel: 'Gateway',
          ideaGenerate: 'Generate',
          ideaGenerating: 'Generating…',
          ideaLabel: 'Idea',
          ideaPlaceholder: 'What are you building?',
          ideaShuffle: 'Shuffle ideas',
          namePlaceholder: 'Project name',
          noFolders: 'No folders yet',
          primaryBadge: 'Primary',
          removeFolder: 'Remove folder'
        }
      }
    }
  })
}))

const { $projectDialog } = vi.hoisted(() => {
  const { atom } = require('nanostores') as typeof Nanostores

  return {
    $projectDialog: atom<{ mode: 'create' | 'rename' | 'add-folder'; name?: string; projectId?: string } | null>({
      mode: 'create'
    })
  }
})

const { createProject, pickProjectFolder } = vi.hoisted(() => ({
  createProject: vi.fn(),
  pickProjectFolder: vi.fn(async () => '/Users/test/my-folder')
}))

vi.mock('@/store/projects', () => ({
  $projectDialog,
  addProjectFolder: vi.fn(),
  closeProjectDialog: vi.fn(),
  connectionIdForProjectId: vi.fn(() => null),
  createProject,
  generateProjectIdea: vi.fn(),
  pickProjectFolder,
  renameProject: vi.fn()
}))

vi.mock('@/store/connections', () => {
  const { atom } = require('nanostores') as typeof Nanostores

  return {
    $activeConnectionId: atom('local'),
    $connectionsRegistry: atom({
      activeId: 'local',
      connections: [
        { id: 'local', kind: 'local', label: 'This device', tokenPreview: null, tokenSet: false },
        { id: 'mimir', kind: 'ssh', label: 'mimir', tokenPreview: null, tokenSet: false }
      ]
    })
  }
})

vi.mock('@/store/notifications', () => ({
  notifyError: vi.fn()
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

  it('shows gateway cards when more than one connection is registered', () => {
    render(<ProjectDialog />)

    expect(screen.getByText('Gateway')).toBeTruthy()
    expect(screen.getByRole('button', { name: /This device/ }).getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByRole('button', { name: /mimir/ }).getAttribute('aria-pressed')).toBe('false')
    expect(screen.getByText('Local')).toBeTruthy()
    expect(screen.getByText('SSH')).toBeTruthy()
  })

  it('clears folders and pins pick/create to the chosen gateway card', async () => {
    render(<ProjectDialog />)

    fireEvent.click(screen.getByRole('button', { name: 'Add folder' }))
    await screen.findByRole('button', { name: 'Remove folder' })

    fireEvent.click(screen.getByRole('button', { name: /mimir/ }))
    expect(screen.queryByRole('button', { name: 'Remove folder' })).toBeNull()
    expect(screen.getByRole('button', { name: /mimir/ }).getAttribute('aria-pressed')).toBe('true')

    pickProjectFolder.mockResolvedValueOnce('/mimir/work')
    fireEvent.click(screen.getByRole('button', { name: 'Add folder' }))
    await screen.findByRole('button', { name: 'Remove folder' })

    expect(pickProjectFolder).toHaveBeenLastCalledWith('mimir')

    fireEvent.change(screen.getByPlaceholderText('Project name'), { target: { value: 'Remote proj' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    await vi.waitFor(() => {
      expect(createProject).toHaveBeenCalledWith(
        expect.objectContaining({
          connectionId: 'mimir',
          folders: ['/mimir/work'],
          name: 'Remote proj'
        })
      )
    })
  })
})
