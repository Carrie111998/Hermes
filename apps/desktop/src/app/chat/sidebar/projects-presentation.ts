import { useMemo, useSyncExternalStore } from 'react'

import { useContributions } from '@/contrib/react/use-contributions'
import { registry } from '@/contrib/registry'

import type { SidebarProjectTree } from './projects'

export const PROJECTS_GROUPING_AREA = 'projects.grouping'

export interface ProjectGroupDescriptor {
  readonly id: string
  readonly label: string
  readonly projectIds: readonly string[]
  readonly collapsed?: boolean
}

export interface ProjectsGroupingSnapshot {
  readonly groups: readonly ProjectGroupDescriptor[]
  /** Opaque provider state identity for capability changes not represented by groups. */
  readonly revision?: number | string
}

export interface DeleteProjectGroupRequest {
  readonly groupId: string
  readonly expectedProjectIds: readonly string[]
  readonly operationId: string
}

export interface ProjectsGroupingContribution {
  /** Keep the returned reference stable until the provider state changes.
   *  The host also reuses structurally equivalent snapshots defensively. */
  getSnapshot(): ProjectsGroupingSnapshot
  subscribe(listener: () => void): () => void
  createGroup?(name: string): Promise<void> | void
  deleteGroup?(request: DeleteProjectGroupRequest): Promise<void>
  assignProject?(projectId: string, groupId: string | null): Promise<void> | void
  setGroupCollapsed?(groupId: string, collapsed: boolean): Promise<void> | void
}

export interface PresentedProjectGroup {
  readonly id: string
  readonly label: string
  readonly projects: readonly SidebarProjectTree[]
  readonly collapsed: boolean
}

export interface PresentedProjectsGrouping {
  readonly contribution: ProjectsGroupingContribution
  readonly snapshot: ProjectsGroupingSnapshot
  readonly home?: SidebarProjectTree
  readonly groups: readonly PresentedProjectGroup[]
  readonly ungrouped: readonly SidebarProjectTree[]
}

function sanitizeSnapshot(value: unknown): ProjectsGroupingSnapshot | null {
  if (!value || typeof value !== 'object' || !Array.isArray((value as Partial<ProjectsGroupingSnapshot>).groups)) {
    return null
  }

  const groups: ProjectGroupDescriptor[] = []

  for (const raw of (value as { groups: readonly unknown[] }).groups) {
    if (!raw || typeof raw !== 'object') {
      return null
    }

    const group = raw as Partial<ProjectGroupDescriptor>

    if (
      typeof group.id !== 'string' ||
      !group.id.trim() ||
      typeof group.label !== 'string' ||
      !group.label.trim() ||
      !Array.isArray(group.projectIds) ||
      !group.projectIds.every(projectId => typeof projectId === 'string')
    ) {
      return null
    }

    groups.push({
      id: group.id.trim(),
      label: group.label.trim(),
      projectIds: group.projectIds.map(projectId => projectId.trim()).filter(Boolean),
      ...(group.collapsed === true && { collapsed: true })
    })
  }

  const revision = (value as { revision?: unknown }).revision
  const validRevision = typeof revision === 'string' || (typeof revision === 'number' && Number.isFinite(revision))

  return { groups, ...(validRevision && { revision }) }
}

function readValidProvider(): {
  contribution: ProjectsGroupingContribution
  snapshot: ProjectsGroupingSnapshot
} | null {
  for (const entry of registry.getArea(PROJECTS_GROUPING_AREA)) {
    const contribution = entry.data as Partial<ProjectsGroupingContribution> | undefined

    if (typeof contribution?.getSnapshot !== 'function' || typeof contribution.subscribe !== 'function') {
      continue
    }

    try {
      const snapshot = sanitizeSnapshot(contribution.getSnapshot())

      if (snapshot) {
        return { contribution: contribution as ProjectsGroupingContribution, snapshot }
      }
    } catch {
      // A malformed provider must not suppress a valid lower-priority provider.
    }
  }

  return null
}

export function resolveProjectsGrouping(projects: readonly SidebarProjectTree[]): PresentedProjectsGrouping | null {
  const active = readValidProvider()

  return active ? resolveProjectsGroupingFrom(active.contribution, active.snapshot, projects) : null
}

function resolveProjectsGroupingFrom(
  contribution: ProjectsGroupingContribution,
  snapshot: ProjectsGroupingSnapshot,
  projects: readonly SidebarProjectTree[]
): PresentedProjectsGrouping {
  const home = projects.find(project => project.isNoProject)
  const realProjects = projects.filter(project => !project.isNoProject)
  const byId = new Map(realProjects.map(project => [project.id, project]))
  const claimed = new Set<string>()
  const groupIds = new Set<string>()
  const groups: PresentedProjectGroup[] = []

  for (const raw of snapshot.groups) {
    const id = typeof raw?.id === 'string' ? raw.id.trim() : ''
    const label = typeof raw?.label === 'string' ? raw.label.trim() : ''

    if (!id || !label || groupIds.has(id) || !Array.isArray(raw?.projectIds)) {
      continue
    }

    groupIds.add(id)
    const groupProjects: SidebarProjectTree[] = []

    for (const rawProjectId of raw.projectIds) {
      if (typeof rawProjectId !== 'string') {
        continue
      }

      const projectId = rawProjectId.trim()

      if (!projectId || claimed.has(projectId)) {
        continue
      }

      const project = byId.get(projectId)

      if (!project) {
        continue
      }

      claimed.add(projectId)
      groupProjects.push(project)
    }

    groups.push({ collapsed: raw.collapsed === true, id, label, projects: groupProjects })
  }

  return {
    contribution,
    groups,
    home,
    snapshot,
    ungrouped: realProjects.filter(project => !claimed.has(project.id))
  }
}

const EMPTY_SUBSCRIBE = () => () => undefined
const NULL_SNAPSHOT = () => null

function snapshotsEqual(left: ProjectsGroupingSnapshot, right: ProjectsGroupingSnapshot): boolean {
  if (left === right) {
    return true
  }

  if (
    left.revision !== right.revision ||
    !Array.isArray(left.groups) ||
    !Array.isArray(right.groups) ||
    left.groups.length !== right.groups.length
  ) {
    return false
  }

  const leftGroups = left.groups as readonly ProjectGroupDescriptor[]
  const rightGroups = right.groups as readonly ProjectGroupDescriptor[]

  return leftGroups.every((group, index) => {
    const other = rightGroups[index]

    if (!group || !other || !Array.isArray(group.projectIds) || !Array.isArray(other.projectIds)) {
      return false
    }

    const projectIds = group.projectIds as readonly string[]
    const otherProjectIds = other.projectIds as readonly string[]

    return (
      group.id === other.id &&
      group.label === other.label &&
      group.collapsed === other.collapsed &&
      projectIds.length === otherProjectIds.length &&
      projectIds.every((projectId, projectIndex) => projectId === otherProjectIds[projectIndex])
    )
  })
}

function createStoreAdapter(contribution: ProjectsGroupingContribution, initialSnapshot: ProjectsGroupingSnapshot) {
  let cachedSnapshot = initialSnapshot

  return {
    getSnapshot: () => {
      let snapshot: ProjectsGroupingSnapshot | null = null

      try {
        snapshot = sanitizeSnapshot(contribution.getSnapshot())
      } catch {
        // Keep the last safe value if an accepted provider later misbehaves.
      }

      if (!snapshot || snapshotsEqual(cachedSnapshot, snapshot)) {
        return cachedSnapshot
      }

      cachedSnapshot = snapshot

      return snapshot
    },
    subscribe: (listener: () => void) => contribution.subscribe(listener)
  }
}

function useProjectsGroupingStore() {
  useContributions(PROJECTS_GROUPING_AREA)
  const active = readValidProvider()
  const contribution = active?.contribution ?? null

  const store = useMemo(
    () => (contribution && active ? createStoreAdapter(contribution, active.snapshot) : null),
    [contribution]
  )

  const snapshot = useSyncExternalStore(
    store?.subscribe ?? EMPTY_SUBSCRIBE,
    store?.getSnapshot ?? NULL_SNAPSHOT,
    store?.getSnapshot ?? NULL_SNAPSHOT
  )

  return contribution && snapshot && Array.isArray(snapshot.groups) ? { contribution, snapshot } : null
}

export function useProjectsGrouping(projects: readonly SidebarProjectTree[]): PresentedProjectsGrouping | null {
  const active = useProjectsGroupingStore()

  return active ? resolveProjectsGroupingFrom(active.contribution, active.snapshot, projects) : null
}

export function useActiveProjectsGrouping() {
  return useProjectsGroupingStore()
}
