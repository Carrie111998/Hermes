import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { SidebarProjectTree } from './projects'
import { SessionsHeaderAction } from './sessions-header-action'

afterEach(cleanup)

const LABELS = { newProject: 'New project', newSession: 'New session', showProjects: 'Show projects' }

const HOME_PROJECT: SidebarProjectTree = {
  id: 'no-project',
  isNoProject: true,
  label: 'Home',
  path: null,
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
