import type { ProjectNameCAS } from '@/store/projects'

import type { ProjectGroupDescriptor, ProjectsGroupingContribution } from './projects-presentation'

export interface ProjectGroupDeleteProject {
  readonly id: string
  readonly name: string
}

export type ProjectGroupDeleteValidationReason = 'collision' | 'invalid' | 'stale' | 'unsupported'

export class ProjectGroupDeleteValidationError extends Error {
  constructor(
    readonly reason: ProjectGroupDeleteValidationReason,
    message: string
  ) {
    super(message)
    this.name = 'ProjectGroupDeleteValidationError'
  }
}

export class ProjectGroupDeleteCompensationError extends Error {
  readonly affectedProjects: readonly ProjectGroupDeleteProject[]

  constructor(
    readonly providerError: unknown,
    readonly rollbackError: unknown,
    affectedProjects: readonly ProjectGroupDeleteProject[],
    readonly reconcileError?: unknown
  ) {
    const names = affectedProjects.map(project => project.name).join(', ')
    super(`Project group deletion failed and Project name rollback also failed: ${names}`)
    this.name = 'ProjectGroupDeleteCompensationError'
    this.affectedProjects = affectedProjects
  }
}

function normalizedMemberIds(projectIds: readonly string[]): string[] {
  const result: string[] = []
  const seen = new Set<string>()

  for (const rawId of projectIds) {
    const id = typeof rawId === 'string' ? rawId.trim() : ''

    if (!id || seen.has(id)) {
      continue
    }

    seen.add(id)
    result.push(id)
  }

  return result
}

function sameMemberSet(left: readonly string[], right: readonly string[]): boolean {
  const leftIds = normalizedMemberIds(left)
  const rightIds = new Set(normalizedMemberIds(right))

  return leftIds.length === rightIds.size && leftIds.every(id => rightIds.has(id))
}

export function buildProjectGroupRenamePlan(
  group: ProjectGroupDescriptor,
  projects: readonly ProjectGroupDeleteProject[]
): ProjectNameCAS[] {
  const groupName = group.label.trim()

  if (!groupName) {
    throw new ProjectGroupDeleteValidationError('invalid', 'Project group name is empty')
  }

  const byId = new Map(projects.map(project => [project.id, project]))
  const prefix = `${groupName} · `
  const lowerPrefix = prefix.toLowerCase()

  const renames = normalizedMemberIds(group.projectIds).map(id => {
    const project = byId.get(id)

    if (!project) {
      throw new ProjectGroupDeleteValidationError('stale', `Project ${id} is missing from the deletion review`)
    }

    const trimmedName = project.name.trim()

    if (!trimmedName) {
      throw new ProjectGroupDeleteValidationError('invalid', `Project ${id} has an empty name`)
    }

    return {
      expectedName: project.name,
      id,
      newName: trimmedName.toLowerCase().startsWith(lowerPrefix) ? trimmedName : `${prefix}${trimmedName}`
    }
  })

  const finalNames = new Map(projects.map(project => [project.id, project.name.trim()]))

  for (const rename of renames) {
    finalNames.set(rename.id, rename.newName)
  }

  const renamedIds = new Set(renames.map(rename => rename.id))
  const idsByName = new Map<string, string[]>()

  for (const [id, name] of finalNames) {
    const key = name.toLowerCase()
    idsByName.set(key, [...(idsByName.get(key) ?? []), id])
  }

  const collision = [...idsByName.entries()].find(([, ids]) => ids.length > 1 && ids.some(id => renamedIds.has(id)))

  if (collision) {
    throw new ProjectGroupDeleteValidationError(
      'collision',
      `Project name collision for ${finalNames.get(collision[1][0]) ?? collision[0]}`
    )
  }

  return renames
}

export interface DeleteProjectGroupOptions {
  readonly contribution: ProjectsGroupingContribution
  readonly group: ProjectGroupDescriptor
  readonly operationId: string
  readonly prependGroupName: boolean
  readonly projects: readonly ProjectGroupDeleteProject[]
  readonly renameMany: (renames: readonly ProjectNameCAS[]) => Promise<unknown>
  readonly reconcile: () => Promise<unknown>
}

/**
 * Cross-authority deletion saga: validate the provider snapshot, CAS-rename
 * Projects, delete provider membership, then CAS-compensate names on failure.
 * A failed compensation is explicit and always triggers authoritative refresh.
 */
export async function deleteProjectGroup({
  contribution,
  group,
  operationId,
  prependGroupName,
  projects,
  renameMany,
  reconcile
}: DeleteProjectGroupOptions): Promise<void> {
  if (!contribution.deleteGroup) {
    throw new ProjectGroupDeleteValidationError('unsupported', 'The Project grouping provider cannot delete groups')
  }

  let currentGroup: ProjectGroupDescriptor | undefined

  try {
    currentGroup = contribution.getSnapshot().groups.find(candidate => candidate.id === group.id)
  } catch {
    throw new ProjectGroupDeleteValidationError('stale', 'The Project group snapshot could not be refreshed')
  }

  if (
    !currentGroup ||
    currentGroup.label.trim() !== group.label.trim() ||
    !sameMemberSet(currentGroup.projectIds, group.projectIds)
  ) {
    throw new ProjectGroupDeleteValidationError('stale', 'The Project group changed since the deletion review')
  }

  const expectedProjectIds = normalizedMemberIds(group.projectIds)
  const renames = prependGroupName ? buildProjectGroupRenamePlan(group, projects) : []

  if (renames.length) {
    await renameMany(renames)
  }

  try {
    await contribution.deleteGroup({ expectedProjectIds, groupId: group.id, operationId })
  } catch (providerError) {
    if (!renames.length) {
      throw providerError
    }

    const rollback = renames.map(rename => ({
      expectedName: rename.newName,
      id: rename.id,
      newName: rename.expectedName
    }))

    try {
      await renameMany(rollback)
    } catch (rollbackError) {
      let reconcileError: unknown

      try {
        await reconcile()
      } catch (error) {
        reconcileError = error
      }

      throw new ProjectGroupDeleteCompensationError(
        providerError,
        rollbackError,
        renames.map(rename => ({ id: rename.id, name: rename.newName })),
        reconcileError
      )
    }

    throw providerError
  }
}
