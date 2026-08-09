// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { SidebarProvider } from '@/components/ui/sidebar'
import {
  $panesFlipped,
  $pinnedSessionIds,
  $sidebarAgentsGrouped,
  $sidebarPinsOpen,
  $sidebarRecentsOpen,
  $sidebarSessionOrderIds,
  $sidebarSessionOrderManual
} from '@/store/layout'
import { $activeGatewayProfile, $profiles, $showAllProfiles } from '@/store/profile'
import {
  $activeProjectId,
  $projects,
  $projectScope,
  $projectTree,
  $projectTreeLoading,
  $removedSessionIds,
  $reposScanning,
  ALL_PROJECTS
} from '@/store/projects'
import {
  $cronSessions,
  $gatewayState,
  $messagingPlatformTotals,
  $messagingSessions,
  $messagingTruncated,
  $sessionProfileTotals,
  $sessions,
  $sessionsLoading,
  $sessionsTotal
} from '@/store/session'
import { $sessionStates } from '@/store/session-states'
import type { SessionInfo } from '@/types/hermes'

import { ChatSidebar } from './index'

vi.mock('./sessions-section', () => ({
  VIRTUALIZE_THRESHOLD: 25,
  SidebarSessionsSection: ({ headerAction, label, onToggle, open, projectOverview, rootClassName, sessions }: {
    headerAction?: ReactNode
    label: string
    onToggle: () => void
    open: boolean
    projectOverview?: Array<{ id: string; label: string }>
    rootClassName?: string
    sessions: SessionInfo[]
  }) => (
    <section aria-label={label} className={rootClassName}>
      <button aria-expanded={open} aria-label={`Toggle ${label}`} onClick={onToggle} type="button" />
      {headerAction}
      {sessions.map(session => (
        <span key={session.id}>{session.title ?? session.id}</span>
      ))}
      {projectOverview?.map(project => <span key={project.id}>{project.label}</span>)}
    </section>
  )
}))

vi.mock('./profile-switcher', () => ({ ProfileRail: () => null }))
vi.mock('./project-dialog', () => ({ ProjectDialog: () => null }))

const noop = () => undefined

function makeSession(overrides: Partial<SessionInfo> = {}): SessionInfo {
  return {
    archived: false,
    cwd: null,
    ended_at: null,
    id: '20260808_191530_b67b9d',
    input_tokens: 0,
    is_active: false,
    last_active: 1_000,
    message_count: 94,
    model: null,
    output_tokens: 0,
    preview: null,
    source: 'desktop',
    started_at: 1_000,
    title: null,
    tool_call_count: 0,
    ...overrides
  }
}

function renderSidebar(): void {
  render(
    <MemoryRouter>
      <SidebarProvider>
        <ChatSidebar
          currentView="chat"
          onArchiveSession={noop}
          onBranchSession={noop}
          onDeleteSession={noop}
          onLoadMoreSessions={noop}
          onManageCronJob={noop}
          onNavigate={noop}
          onNewSessionInWorkspace={noop}
          onNewSessionSplit={noop}
          onResumeSession={noop}
          onTriggerCronJob={noop}
        />
      </SidebarProvider>
    </MemoryRouter>
  )
}

beforeEach(() => {
  window.localStorage.clear()
  $panesFlipped.set(false)
  $pinnedSessionIds.set([])
  $sidebarAgentsGrouped.set(true)
  $sidebarPinsOpen.set(true)
  $sidebarRecentsOpen.set(true)
  $sidebarSessionOrderIds.set([])
  $sidebarSessionOrderManual.set(false)

  $profiles.set([])
  $showAllProfiles.set(false)
  $activeGatewayProfile.set('default')

  $activeProjectId.set(null)
  $projectScope.set(ALL_PROJECTS)
  $projects.set([])
  $projectTree.set([
    {
      color: null,
      icon: null,
      id: 'unrelated-project',
      isAuto: true,
      label: 'Unrelated project',
      path: 'C:/work/unrelated',
      previewSessions: [],
      repos: [],
      sessionCount: 0
    }
  ])
  $projectTreeLoading.set(false)
  $removedSessionIds.set(new Set())
  $reposScanning.set(false)

  $cronSessions.set([])
  $gatewayState.set('idle')
  $messagingPlatformTotals.set({})
  $messagingSessions.set([])
  $messagingTruncated.set(false)
  $sessionProfileTotals.set({ default: 1 })
  $sessions.set([makeSession()])
  $sessionsLoading.set(false)
  $sessionsTotal.set(1)
  $sessionStates.set({})
})

afterEach(cleanup)

describe('ChatSidebar session visibility', () => {
  it('keeps every conversational session in Sessions while Projects is also visible', () => {
    renderSidebar()

    expect(screen.getByRole('region', { name: 'Projects' })).toBeTruthy()
    expect(screen.getByRole('region', { name: 'Sessions' }).textContent).toContain('20260808_191530_b67b9d')
  })

  it('releases Projects flex space when collapsed while Sessions remains visible', () => {
    renderSidebar()

    const projectsRoot = screen.getByRole('region', { name: 'Projects' })

    expect(projectsRoot.classList.contains('flex-1')).toBe(true)
    expect(projectsRoot.classList.contains('min-h-32')).toBe(true)
    expect(projectsRoot.classList.contains('overflow-hidden')).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: 'Toggle Projects' }))

    expect(projectsRoot.classList.contains('flex-1')).toBe(false)
    expect(projectsRoot.classList.contains('min-h-32')).toBe(false)
    expect(projectsRoot.classList.contains('overflow-hidden')).toBe(false)
    expect(projectsRoot.classList.contains('flex-none')).toBe(true)
    expect(projectsRoot.classList.contains('overflow-visible')).toBe(true)
    expect(screen.getByRole('button', { name: 'Toggle Projects' }).getAttribute('aria-expanded')).toBe('false')
    expect(screen.getByRole('region', { name: 'Sessions' }).textContent).toContain('20260808_191530_b67b9d')
  })

  it('keeps Sessions visible while a project is entered', () => {
    $projectScope.set('unrelated-project')

    renderSidebar()

    expect(screen.getByRole('region', { name: 'Unrelated project' })).toBeTruthy()
    expect(screen.getByRole('region', { name: 'Sessions' }).textContent).toContain('20260808_191530_b67b9d')
  })

  it('never replaces Sessions when the Projects view is toggled', () => {
    renderSidebar()

    fireEvent.click(screen.getByRole('button', { name: 'Show sessions' }))

    expect(screen.queryByRole('region', { name: 'Projects' })).toBeNull()
    expect(screen.getByRole('region', { name: 'Sessions' }).textContent).toContain('20260808_191530_b67b9d')

    fireEvent.click(screen.getByRole('button', { name: 'Show projects' }))

    expect(screen.getByRole('region', { name: 'Projects' })).toBeTruthy()
    expect(screen.getByRole('region', { name: 'Sessions' }).textContent).toContain('20260808_191530_b67b9d')
  })
})
