import { describe, expect, it } from 'vitest'

import type { SidebarProjectTree } from '@/app/chat/sidebar/projects/workspace-groups'
import type { ProjectInfo, SessionInfo } from '@/hermes'

import { selectProjectWorkspaceSessions } from './projection'

const session = (id: string, cwd: string, lastActive: number, source = 'cli'): SessionInfo => ({
  id,
  cwd,
  last_active: lastActive,
  preview: id,
  source,
  started_at: lastActive
} as SessionInfo)

const projectInfo: ProjectInfo = {
  id: 'p_kiwi',
  name: '키위스튜디오',
  folders: [{ path: '/repo/kiwi' }],
  primary_path: '/repo/kiwi'
} as ProjectInfo

const hydrated: SidebarProjectTree = {
  id: 'p_kiwi',
  label: '키위스튜디오',
  path: '/repo/kiwi',
  sessionCount: 1,
  repos: [{
    id: '/repo/kiwi',
    label: 'kiwi',
    path: '/repo/kiwi',
    sessionCount: 1,
    groups: [{
      id: '/repo/kiwi::branch::main',
      label: 'main',
      path: '/repo/kiwi',
      sessions: [session('persisted', '/repo/kiwi', 10, 'slack')]
    }]
  }]
}

describe('selectProjectWorkspaceSessions', () => {
  it('combines authoritative project rows with same-project live rows only', () => {
    const result = selectProjectWorkspaceSessions({
      allSessions: [
        session('persisted', '/repo/kiwi', 10, 'slack'),
        session('live', '/repo/kiwi/apps/web', 30),
        session('other', '/repo/other', 40)
      ],
      hydratedProject: hydrated,
      projectId: 'p_kiwi',
      projects: [projectInfo]
    })

    expect(result.map(row => row.id)).toEqual(['live', 'persisted'])
    expect(result.filter(row => row.id === 'persisted')).toHaveLength(1)
  })

  it('does not leak another hydrated project or unscoped sessions', () => {
    expect(selectProjectWorkspaceSessions({
      allSessions: [session('other', '/repo/other', 40)],
      hydratedProject: { ...hydrated, id: 'p_other' },
      projectId: 'p_kiwi',
      projects: [projectInfo]
    })).toEqual([])
  })

  it('returns no rows when no project is selected', () => {
    expect(selectProjectWorkspaceSessions({
      allSessions: [session('persisted', '/repo/kiwi', 10)],
      hydratedProject: hydrated,
      projectId: null,
      projects: [projectInfo]
    })).toEqual([])
  })
})
