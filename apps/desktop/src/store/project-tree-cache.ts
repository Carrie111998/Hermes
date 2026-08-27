import type { SidebarProjectTree } from '@/app/chat/sidebar/projects/workspace-groups'
import { readKey, writeKey } from '@/lib/storage'

const STORAGE_KEY = 'hermes.desktop.projectTreeCache.v1'
const MAX_CONTEXTS = 8
const MAX_AGE_MS = 14 * 24 * 60 * 60 * 1000

export interface ProjectTreeCacheScope {
  connectionId: string
  profile: string
}

export interface ProjectTreeSnapshot {
  activeId: null | string
  projects: SidebarProjectTree[]
}

interface StoredProjectTreeSnapshot extends ProjectTreeSnapshot {
  savedAt: number
}

type ProjectTreeCache = Record<string, StoredProjectTreeSnapshot>

const isRecord = (value: unknown): value is Record<string, unknown> =>
  Boolean(value) && typeof value === 'object' && !Array.isArray(value)

function isProjectTree(value: unknown): value is SidebarProjectTree {
  if (!isRecord(value) || typeof value.id !== 'string' || typeof value.label !== 'string' || !Array.isArray(value.repos)) {
    return false
  }

  return value.repos.every(
    repo =>
      isRecord(repo) &&
      typeof repo.id === 'string' &&
      typeof repo.label === 'string' &&
      Array.isArray(repo.groups) &&
      repo.groups.every(
        group =>
          isRecord(group) &&
          typeof group.id === 'string' &&
          typeof group.label === 'string' &&
          Array.isArray(group.sessions)
      )
  )
}

function isStoredSnapshot(value: unknown, now: number): value is StoredProjectTreeSnapshot {
  return (
    isRecord(value) &&
    (value.activeId === null || typeof value.activeId === 'string') &&
    typeof value.savedAt === 'number' &&
    Number.isFinite(value.savedAt) &&
    now - value.savedAt <= MAX_AGE_MS &&
    Array.isArray(value.projects) &&
    value.projects.every(isProjectTree)
  )
}

function readCache(): ProjectTreeCache {
  const raw = readKey(STORAGE_KEY)

  if (!raw) {
    return {}
  }

  try {
    const parsed = JSON.parse(raw) as unknown

    if (!isRecord(parsed)) {
      return {}
    }

    const now = Date.now()

    return Object.fromEntries(
      Object.entries(parsed)
        .filter((entry): entry is [string, StoredProjectTreeSnapshot] => isStoredSnapshot(entry[1], now))
        .sort((left, right) => right[1].savedAt - left[1].savedAt)
        .slice(0, MAX_CONTEXTS)
    )
  } catch {
    return {}
  }
}

export function projectTreeCacheScopeKey(scope: ProjectTreeCacheScope): string {
  return JSON.stringify([scope.connectionId.trim() || 'local', scope.profile.trim() || 'default'])
}

export function readProjectTreeSnapshot(scope: ProjectTreeCacheScope): ProjectTreeSnapshot | null {
  const cached = readCache()[projectTreeCacheScopeKey(scope)]

  if (!cached) {
    return null
  }

  const { savedAt: _savedAt, ...snapshot } = cached

  return snapshot
}

export function writeProjectTreeSnapshot(scope: ProjectTreeCacheScope, snapshot: ProjectTreeSnapshot): void {
  const key = projectTreeCacheScopeKey(scope)
  const cache = readCache()

  cache[key] = { ...snapshot, savedAt: Date.now() }

  writeKey(
    STORAGE_KEY,
    JSON.stringify(
      Object.fromEntries(
        Object.entries(cache)
          .sort((left, right) => right[1].savedAt - left[1].savedAt)
          .slice(0, MAX_CONTEXTS)
      )
    )
  )
}

export const PROJECT_TREE_CACHE_STORAGE_KEY = STORAGE_KEY
