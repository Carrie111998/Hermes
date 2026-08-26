import { normalizeProfileKey } from '@/store/profile'

/**
 * Qualified project identity across gateways and profiles.
 * Bare `project.id` from projects.db is only unique within one (connection, profile).
 */
export type ProjectAddress = {
  connectionId: string
  profile: string
  backendProjectId: string
}

/** Unit-separator triple — backend ids are paths or `p_*`, not this char. */
const SEP = '\u001f'

export const ALL_PROJECTS_SCOPE = '__all_projects__'
export const NO_PROJECT_SCOPE = '__no_project__'

export function projectAddressKey(addr: ProjectAddress): string {
  return [
    addr.connectionId.trim() || 'local',
    normalizeProfileKey(addr.profile),
    addr.backendProjectId
  ].join(SEP)
}

export function parseProjectAddressKey(raw: string): null | ProjectAddress {
  const value = raw.trim()

  if (!value || value === ALL_PROJECTS_SCOPE || value === NO_PROJECT_SCOPE) {
    return null
  }

  const parts = value.split(SEP)

  if (parts.length !== 3 || !parts[2]) {
    return null
  }

  return {
    connectionId: parts[0].trim() || 'local',
    profile: normalizeProfileKey(parts[1]),
    backendProjectId: parts[2]
  }
}

export function isProjectAddressKey(raw: string): boolean {
  return parseProjectAddressKey(raw) !== null
}

export function sameProjectAddress(a: ProjectAddress, b: ProjectAddress): boolean {
  return (
    (a.connectionId.trim() || 'local') === (b.connectionId.trim() || 'local') &&
    normalizeProfileKey(a.profile) === normalizeProfileKey(b.profile) &&
    a.backendProjectId === b.backendProjectId
  )
}

export function makeProjectAddress(
  connectionId: string,
  profile: string,
  backendProjectId: string
): ProjectAddress {
  return {
    connectionId: connectionId.trim() || 'local',
    profile: normalizeProfileKey(profile),
    backendProjectId
  }
}

/** Scope key from stamped tree fields only (no chrome atom) — safe in presenters. */
export function scopeKeyFromProjectFields(node: {
  id: string
  connectionId?: string
  profile?: string
  isNoProject?: boolean
}): string {
  if (node.isNoProject) {
    return NO_PROJECT_SCOPE
  }

  return projectAddressKey(
    makeProjectAddress(node.connectionId || 'local', node.profile || 'default', node.id)
  )
}
