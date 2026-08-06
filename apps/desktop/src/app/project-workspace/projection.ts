import {
  liveSessionProjectId,
  sessionRecency,
  type SidebarProjectTree
} from '@/app/chat/sidebar/projects/workspace-groups'
import type { ProjectInfo, SessionInfo } from '@/hermes'

interface SelectProjectWorkspaceSessionsInput {
  allSessions: SessionInfo[]
  hydratedProject: null | SidebarProjectTree
  projectId: null | string
  projects: ProjectInfo[]
}

const sessionsInTree = (project: SidebarProjectTree): SessionInfo[] =>
  project.repos.flatMap(repo => repo.groups.flatMap(group => group.sessions))

/**
 * Project membership comes from the hydrated backend tree. The only optimistic
 * addition is a live row that the existing project-tree rules can place in the
 * same project before the next backend snapshot arrives.
 */
export function selectProjectWorkspaceSessions({
  allSessions,
  hydratedProject,
  projectId,
  projects
}: SelectProjectWorkspaceSessionsInput): SessionInfo[] {
  if (!projectId) {
    return []
  }

  const authoritativeRows = hydratedProject?.id === projectId
    ? sessionsInTree(hydratedProject)
    : []

  const authoritativeIds = new Set(authoritativeRows.map(session => session.id))
  const rows = new Map(authoritativeRows.map(session => [session.id, session] as const))

  for (const session of allSessions) {
    if (
      authoritativeIds.has(session.id) ||
      liveSessionProjectId(session, projects) === projectId
    ) {
      rows.set(session.id, session)
    }
  }

  return [...rows.values()].sort((a, b) => sessionRecency(b) - sessionRecency(a))
}
