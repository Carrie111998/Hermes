import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SidebarBlankState } from './section-states'

afterEach(cleanup)

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      sidebar: {
        noSessions: 'No sessions yet',
        projects: { newButton: 'New project' }
      }
    }
  })
}))

describe('SidebarBlankState', () => {
  it('offers project creation when the current profile can own it', () => {
    const onNewProject = vi.fn()

    render(<SidebarBlankState onNewProject={onNewProject} />)
    fireEvent.click(screen.getByRole('button', { name: 'New project' }))

    expect(onNewProject).toHaveBeenCalledOnce()
  })

  it('does not offer an unroutable project write in the all-profiles view', () => {
    render(<SidebarBlankState />)

    expect(screen.queryByRole('button', { name: 'New project' })).toBeNull()
  })
})
