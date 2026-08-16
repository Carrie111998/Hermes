import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { SidebarProjectTree } from './projects'
import { SessionsHeaderAction } from './sessions-header-action'

afterEach(cleanup)

// SessionsHeaderAction only branches on ProjectMenu vs. NewSessionHeaderButton;
// ProjectMenu's own behavior (rename/theme/delete) has its own test file.
vi.mock('./projects', () => ({
  ProjectMenu: () => <div data-testid="project-menu" />,
  StartWorkButton: () => null
}))

const LABELS = { newProject: 'New project', newSession: 'New session', showProjects: 'Show projects' }

const HOME_PROJECT: SidebarProjectTree = {
  id: 'no-project',
  isNoProject: true,
  label: 'Home',
  path: null,
  repos: [],
  sessionCount: 0
}

const REAL_PROJECT: SidebarProjectTree = {
  id: 'p1',
  isNoProject: false,
  label: 'Real project',
  path: '/repo',
  repos: [],
  sessionCount: 0
}

// #83479: entering the synthetic "Home" bucket (a project view with no
// folder of its own) rendered only the "back to projects" button — no way
// to start a new session from there without leaving Home first.
describe('SessionsHeaderAction — Home', () => {
  it('shows a "New session" button when the entered project is Home', () => {
    render(
      <SessionsHeaderAction
        activeProjectId={null}
        agentsGrouped
        enteredProject={HOME_PROJECT}
        inProject
        labels={LABELS}
        onExitProjectScope={vi.fn()}
        onNewSessionInWorkspace={vi.fn()}
        onOpenProjectCreate={vi.fn()}
        showAllProfiles={false}
      />
    )

    expect(screen.getByRole('button', { name: 'New session' })).toBeTruthy()
  })

  // It's the only way to start a session from Home, so it shouldn't be
  // hover-revealed like the flat view's "+" — unlike the "back to projects"
  // button beside it, which uses the same always-visible treatment.
  it('keeps the "New session" button always visible rather than hover-revealed', () => {
    render(
      <SessionsHeaderAction
        activeProjectId={null}
        agentsGrouped
        enteredProject={HOME_PROJECT}
        inProject
        labels={LABELS}
        onExitProjectScope={vi.fn()}
        onNewSessionInWorkspace={vi.fn()}
        onOpenProjectCreate={vi.fn()}
        showAllProfiles={false}
      />
    )

    expect(screen.getByRole('button', { name: 'New session' }).className).not.toMatch(/opacity-0\b/)
  })

  it('starts a new session (path null) when the button is clicked', () => {
    const onNewSessionInWorkspace = vi.fn()

    render(
      <SessionsHeaderAction
        activeProjectId={null}
        agentsGrouped
        enteredProject={HOME_PROJECT}
        inProject
        labels={LABELS}
        onExitProjectScope={vi.fn()}
        onNewSessionInWorkspace={onNewSessionInWorkspace}
        onOpenProjectCreate={vi.fn()}
        showAllProfiles={false}
      />
    )

    screen.getByRole('button', { name: 'New session' }).click()

    expect(onNewSessionInWorkspace).toHaveBeenCalledWith(null)
  })

  it('still shows the "back to projects" button alongside it', () => {
    render(
      <SessionsHeaderAction
        activeProjectId={null}
        agentsGrouped
        enteredProject={HOME_PROJECT}
        inProject
        labels={LABELS}
        onExitProjectScope={vi.fn()}
        onNewSessionInWorkspace={vi.fn()}
        onOpenProjectCreate={vi.fn()}
        showAllProfiles={false}
      />
    )

    expect(screen.getByRole('button', { name: 'Show projects' })).toBeTruthy()
  })
})

// The real-project branch was moved furthest from its original inline form
// during the #83479 refactor, so lock down that it still renders ProjectMenu
// (rename/theme/delete) rather than the Home/flat-view "+" button.
describe('SessionsHeaderAction — real project', () => {
  it('renders ProjectMenu (not the new-session button) when the entered project has a path', () => {
    render(
      <SessionsHeaderAction
        activeProjectId={null}
        agentsGrouped
        enteredProject={REAL_PROJECT}
        inProject
        labels={LABELS}
        onExitProjectScope={vi.fn()}
        onNewSessionInWorkspace={vi.fn()}
        onOpenProjectCreate={vi.fn()}
        showAllProfiles={false}
      />
    )

    expect(screen.getByTestId('project-menu')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'New session' })).toBeNull()
  })
})
