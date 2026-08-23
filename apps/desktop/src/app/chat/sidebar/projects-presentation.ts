import { registry } from '@/contrib/registry'

import type { SidebarProjectTree } from './projects'

export const PROJECTS_PRESENTATION_AREA = 'projects.presentation'

/** One plugin-defined group in the native Projects overview. Project ids that
 * are absent from every group stay in the ordinary ungrouped list. */
export interface ProjectPresentationGroup {
  id: string
  label: string
  projectIds: string[]
  collapsed?: boolean
}

/** A presentation provider is data-only: core keeps ownership of native rows,
 * context menus, DnD, counts, worktrees, and project activation. */
export interface ProjectsPresentationContribution {
  groups: ProjectPresentationGroup[]
}

export interface PresentedProjectGroup {
  id: string
  label: string
  projects: SidebarProjectTree[]
  collapsed: boolean
}

export interface PresentedProjects {
  groups: PresentedProjectGroup[]
  ungrouped: SidebarProjectTree[]
}

/** Apply the first valid provider. A single owner avoids two plugins producing
 * contradictory parentage for the same Project; lower `order` wins through the
 * normal contribution registry ordering. Invalid ids and duplicates fail soft. */
export function presentProjects(projects: SidebarProjectTree[]): PresentedProjects | null {
  const provider = registry.getArea(PROJECTS_PRESENTATION_AREA)[0]
  const data = provider?.data as Partial<ProjectsPresentationContribution> | undefined

  if (!Array.isArray(data?.groups)) {
    return null
  }

  const byId = new Map(projects.map(project => [project.id, project]))
  const claimed = new Set<string>()
  const groupIds = new Set<string>()
  const groups: PresentedProjectGroup[] = []

  for (const raw of data.groups) {
    const id = String(raw?.id ?? '').trim()
    const label = String(raw?.label ?? '').trim()

    if (!id || !label || groupIds.has(id) || !Array.isArray(raw?.projectIds)) {
      continue
    }

    groupIds.add(id)
    const rows: SidebarProjectTree[] = []

    for (const projectId of raw.projectIds) {
      if (typeof projectId !== 'string' || claimed.has(projectId)) {
        continue
      }
      const project = byId.get(projectId)
      if (project) {
        claimed.add(projectId)
        rows.push(project)
      }
    }

    groups.push({ collapsed: raw.collapsed === true, id, label, projects: rows })
  }

  return {
    groups,
    ungrouped: projects.filter(project => !claimed.has(project.id))
  }
}
