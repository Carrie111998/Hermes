import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'

import { SidebarWorkspaceGroup } from './workspace-group'
import type { SidebarSessionGroup } from './workspace-groups'

afterEach(cleanup)

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      sidebar: {
        newSessionIn: (label: string) => `New session in ${label}`,
        noSessions: 'No sessions yet',
        projects: {
          reorder: (label: string) => `Reorder ${label}`,
          toggle: (label: string, open: boolean) => `${open ? 'Show' : 'Hide'} ${label} sessions`
        }
      },
      profiles: {
        switchToProfile: (label: string) => `Switch to ${label}`
      },
      statusStack: {
        coding: {
          switchFailed: () => 'switch failed'
        }
      }
    }
  })
}))

vi.mock('./model', () => ({
  PROJECT_PREVIEW_COUNT: 3,
  SIDEBAR_GROUP_PAGE: 10,
  useWorkspaceNodeOpen: () => [false, vi.fn()]
}))

const group = {
  id: 'edgezenn',
  label: 'edgezenn',
  mode: 'profile',
  path: null,
  sessions: [{ id: 's1' } as unknown as SessionInfo]
} as unknown as SidebarSessionGroup

describe('SidebarWorkspaceGroup reorderable profile header', () => {
  it('renders a grab handle with a reorder aria-label when reorderable', () => {
    render(
      <SidebarWorkspaceGroup
        dragHandleProps={{}}
        group={group}
        renderRows={() => null}
        reorderable
      />
    )

    const handle = screen.getByLabelText('Reorder edgezenn')
    expect(handle.hasAttribute('data-reorder-handle')).toBe(true)
  })

  it('renders the plain lead without a grab handle when not reorderable', () => {
    render(<SidebarWorkspaceGroup group={group} renderRows={() => null} />)

    expect(screen.queryByLabelText('Reorder edgezenn')).toBeNull()
  })

  it('forwards pointer-down on the label as grab surface, keeping row actions out', () => {
    const onPointerDown = vi.fn()

    render(
      <SidebarWorkspaceGroup
        dragHandleProps={{ onPointerDown }}
        group={group}
        renderRows={() => null}
        reorderable
      />
    )

    fireEvent.pointerDown(screen.getByText('edgezenn'))

    expect(onPointerDown).toHaveBeenCalledTimes(1)
  })

  it('keeps the add button outside the grab surface', () => {
    const onPointerDown = vi.fn()

    render(
      <SidebarWorkspaceGroup
        dragHandleProps={{ onPointerDown }}
        group={group}
        renderRows={() => null}
        reorderable
      />
    )

    fireEvent.pointerDown(screen.getByRole('button', { name: 'New session in edgezenn' }))

    expect(onPointerDown).not.toHaveBeenCalled()
  })
})
