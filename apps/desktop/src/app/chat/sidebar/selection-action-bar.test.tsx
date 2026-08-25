import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  clearSessionSelection,
  enterSelectionMode,
  registerBulkSessionActions,
  toggleSessionSelection
} from '@/store/session-selection'

import { SidebarSelectionActionBar } from './selection-action-bar'

afterEach(() => {
  cleanup()
  clearSessionSelection()
  registerBulkSessionActions(null)
})

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      sidebar: {
        selection: {
          archive: 'Archive',
          cancel: 'Cancel',
          count: (n: number) => `${n} selected`,
          delete: 'Delete',
          deleteDesc: (n: number) => `This will delete ${n} chats.`,
          deleteTitle: (n: number) => `Delete ${n} chats?`
        }
      },
      common: { cancel: 'Cancel', confirm: 'Confirm', delete: 'Delete', done: 'Done', loading: 'Loading…' }
    }
  })
}))

describe('SidebarSelectionActionBar', () => {
  it('renders nothing when selection mode is inactive', () => {
    const { container } = render(<SidebarSelectionActionBar />)

    expect(container.innerHTML).toBe('')
  })

  it('shows the selected count and calls bulk archive with the selection', () => {
    const archive = vi.fn()
    registerBulkSessionActions({ archive, remove: vi.fn() })
    enterSelectionMode('a')
    toggleSessionSelection('test', 'b')

    render(<SidebarSelectionActionBar />)

    expect(screen.getByText('2 selected')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Archive' }))
    expect(archive).toHaveBeenCalledWith(['a', 'b'])
  })

  it('gates delete behind a confirm dialog naming the count', async () => {
    const remove = vi.fn()
    registerBulkSessionActions({ archive: vi.fn(), remove })
    enterSelectionMode('a')
    toggleSessionSelection('test', 'b')

    render(<SidebarSelectionActionBar />)

    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Delete 2 chats?')).toBeTruthy()
    expect(remove).not.toHaveBeenCalled()

    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }))
    expect(remove).toHaveBeenCalledWith(['a', 'b'])
  })

  it('clears the selection on Cancel', () => {
    registerBulkSessionActions({ archive: vi.fn(), remove: vi.fn() })
    enterSelectionMode('a')

    render(<SidebarSelectionActionBar />)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(screen.queryByText('1 selected')).toBeNull()
  })

  it('disables Archive and Delete while no bulk actions are registered', () => {
    enterSelectionMode('a')

    render(<SidebarSelectionActionBar />)

    expect(screen.getByRole('button', { name: 'Archive' }).hasAttribute('disabled')).toBe(true)
    expect(screen.getByRole('button', { name: 'Delete' }).hasAttribute('disabled')).toBe(true)
  })
})
