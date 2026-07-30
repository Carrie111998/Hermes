import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { StartWorkButton, WorkspaceAddButton, WorkspaceMenu, WorkspaceShowMoreButton } from './workspace-header'

const isDesktopFsRemoteMode = vi.hoisted(() => vi.fn(() => false))

afterEach(cleanup)

beforeEach(() => {
  isDesktopFsRemoteMode.mockReturnValue(false)
})

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      sidebar: {
        projects: {
          copyPath: 'Copy path',
          menu: 'Actions',
          removeWorktree: 'Remove worktree',
          reveal: 'Reveal in file manager',
          startWork: 'New worktree'
        },
        showMoreIn: (n: number, label: string) => `Show ${n} more in ${label}`
      }
    }
  })
}))

vi.mock('@/store/projects', () => ({
  copyPath: vi.fn(),
  revealPath: vi.fn()
}))

vi.mock('@/lib/desktop-fs', () => ({
  isDesktopFsRemoteMode
}))

// StartWorkButton renders the full WorktreeDialog (branch picker, git combobox,
// etc.) as soon as it's open — none of that is relevant to the tooltip fix, so
// stub it to keep this test focused on the trigger button.
vi.mock('./worktree-dialog', () => ({
  WorktreeDialog: () => null
}))

const tipTrigger = (button: HTMLElement) => button.closest('[data-slot="tooltip-trigger"]')

const openTriggerMenu = (trigger: HTMLElement) => {
  fireEvent.pointerDown(trigger, { button: 0, pointerType: 'mouse' })
  fireEvent.pointerUp(trigger, { button: 0, pointerType: 'mouse' })
  fireEvent.click(trigger)
}

describe('WorkspaceAddButton', () => {
  it('wraps the "+" button in a Tip', () => {
    render(<WorkspaceAddButton label="New session in Test D" onClick={vi.fn()} />)

    const button = screen.getByRole('button', { name: 'New session in Test D' })
    expect(tipTrigger(button)).toBeTruthy()
  })

  it('still fires onClick', () => {
    const onClick = vi.fn()
    render(<WorkspaceAddButton label="New session in Test D" onClick={onClick} />)

    fireEvent.click(screen.getByRole('button', { name: 'New session in Test D' }))
    expect(onClick).toHaveBeenCalledOnce()
  })
})

describe('WorkspaceShowMoreButton', () => {
  it('wraps the ellipsis button in a Tip with the composed label', () => {
    render(<WorkspaceShowMoreButton count={5} label="Test D" onClick={vi.fn()} />)

    const button = screen.getByRole('button', { name: 'Show 5 more in Test D' })
    expect(tipTrigger(button)).toBeTruthy()
  })
})

describe('WorkspaceMenu', () => {
  it('does not wrap the kebab trigger in a Tip', () => {
    render(<WorkspaceMenu onRemove={vi.fn()} path="/repo/lane" />)

    const button = screen.getByRole('button', { name: 'Actions' })
    expect(tipTrigger(button)).toBeNull()
  })

  it('keeps reveal and copy path actions in local mode', async () => {
    render(<WorkspaceMenu onRemove={vi.fn()} path="/repo/lane" />)

    openTriggerMenu(screen.getByRole('button', { name: 'Actions' }))

    expect(await screen.findByRole('menuitem', { name: 'Reveal in file manager' })).toBeTruthy()
    expect(screen.getByRole('menuitem', { name: 'Copy path' })).toBeTruthy()
  })

  it('hides reveal but keeps copy path in remote mode', async () => {
    isDesktopFsRemoteMode.mockReturnValue(true)
    render(<WorkspaceMenu onRemove={vi.fn()} path="/repo/lane" />)

    openTriggerMenu(screen.getByRole('button', { name: 'Actions' }))

    expect(await screen.findByRole('menuitem', { name: 'Copy path' })).toBeTruthy()
    expect(screen.queryByRole('menuitem', { name: 'Reveal in file manager' })).toBeNull()
  })
})

describe('StartWorkButton', () => {
  it('wraps the git-branch trigger in a Tip', () => {
    render(<StartWorkButton onStarted={vi.fn()} repoPath="/repo" />)

    const button = screen.getByRole('button', { name: 'New worktree' })
    expect(tipTrigger(button)).toBeTruthy()
  })
})
