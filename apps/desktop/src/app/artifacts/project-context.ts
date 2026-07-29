import type { SidebarProjectTree } from '@/app/chat/sidebar/projects/workspace-groups'
import { enterProjectWorkspace } from '@/store/projects'

/** A concrete project chosen on the Artifacts page becomes the Desktop's
 * actual project context, rather than remaining a page-local filter. */
export function enterArtifactProject(projectId: string, projects: readonly SidebarProjectTree[]): boolean {
  const project = projects.find(candidate => candidate.id === projectId)

  if (!project) {
    return false
  }

  enterProjectWorkspace(project)

  return true
}
