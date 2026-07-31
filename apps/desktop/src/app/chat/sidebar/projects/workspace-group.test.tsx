import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SidebarWorkspaceGroup } from './workspace-group'

const { switchBranchInRepo } = vi.hoisted(() => ({ switchBranchInRepo: vi.fn() }))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      sidebar: {
        newSessionIn: (label: string) => `New session in ${label}`,
        noSessions: 'No sessions'
      },
      statusStack: { coding: { switchFailed: (branch: string) => `Failed to switch ${branch}` } }
    }
  })
}))
vi.mock('@/store/layout', () => ({ setWorkspaceNodeOpen: vi.fn() }))
vi.mock('@/store/notifications', () => ({ notifyError: vi.fn() }))
vi.mock('@/store/profile', () => ({ newSessionInProfile: vi.fn() }))
vi.mock('@/store/projects', () => ({ switchBranchInRepo }))
vi.mock('../chrome', () => ({ SidebarRowStack: ({ children }: { children: React.ReactNode }) => <div>{children}</div> }))
vi.mock('../load-more-row', () => ({ SidebarLoadMoreRow: () => null }))
vi.mock('./model', () => ({ SIDEBAR_GROUP_PAGE: 20, useWorkspaceNodeOpen: () => [true, vi.fn()] }))
vi.mock('./workspace-header', () => ({
  WorkspaceAddButton: ({ label, onClick }: { label: string; onClick: () => void }) => (
    <button onClick={onClick} type="button">
      {label}
    </button>
  ),
  WorkspaceContextMenu: ({ children }: { children: React.ReactNode }) => children,
  WorkspaceHeader: ({ action }: { action: React.ReactNode }) => <div>{action}</div>,
  WorkspaceMenu: () => null,
  WorkspaceShowMoreButton: () => null
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('SidebarWorkspaceGroup main-checkout session creation', () => {
  it('switches and starts the session in the authoritative repo root', async () => {
    const onNewSession = vi.fn()

    render(
      <SidebarWorkspaceGroup
        group={{ id: 'main', isMain: true, label: 'main', path: '/project/container', sessions: [] }}
        onNewSession={onNewSession}
        renderRows={() => null}
        repoPath="/actual/repo"
      />
    )

    fireEvent.click(screen.getByRole('button', { name: 'New session in main' }))

    await waitFor(() => expect(switchBranchInRepo).toHaveBeenCalledWith('/actual/repo', 'main'))
    expect(onNewSession).toHaveBeenCalledWith('/actual/repo')
  })
})
