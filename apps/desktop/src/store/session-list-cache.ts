import { readKey, writeKey } from '@/lib/storage'
import type { SessionInfo } from '@/types/hermes'

const STORAGE_KEY = 'hermes.desktop.sidebarSessionsCache.v1'
const MAX_CONTEXTS = 8
const MAX_AGE_MS = 14 * 24 * 60 * 60 * 1000
const MAX_RECENTS = 350
const MAX_CRON = 100
const MAX_MESSAGING = 250

export interface SessionListCacheScope {
  connectionId: null | string
  profile: string
}

export interface SessionListSnapshot {
  cron: SessionInfo[]
  messaging: SessionInfo[]
  messagingTruncated: boolean
  profilesTruncated: Record<string, boolean>
  profilesUsage: Record<string, { cost_usd: number; tokens: number }>
  recents: SessionInfo[]
}

interface StoredSessionListSnapshot extends SessionListSnapshot {
  savedAt: number
}

type SessionListCache = Record<string, StoredSessionListSnapshot>

const isRecord = (value: unknown): value is Record<string, unknown> =>
  Boolean(value) && typeof value === 'object' && !Array.isArray(value)

function isSessionInfo(value: unknown): value is SessionInfo {
  return (
    isRecord(value) &&
    typeof value.id === 'string' &&
    value.id.length > 0 &&
    typeof value.last_active === 'number' &&
    Number.isFinite(value.last_active) &&
    (value.profile === undefined || typeof value.profile === 'string') &&
    (value.connection_id === undefined || typeof value.connection_id === 'string')
  )
}

function isBooleanRecord(value: unknown): value is Record<string, boolean> {
  return isRecord(value) && Object.values(value).every(item => typeof item === 'boolean')
}

function isUsageRecord(value: unknown): value is Record<string, { cost_usd: number; tokens: number }> {
  return (
    isRecord(value) &&
    Object.values(value).every(
      item =>
        isRecord(item) &&
        typeof item.cost_usd === 'number' &&
        Number.isFinite(item.cost_usd) &&
        typeof item.tokens === 'number' &&
        Number.isFinite(item.tokens)
    )
  )
}

function isStoredSnapshot(value: unknown, now: number): value is StoredSessionListSnapshot {
  return (
    isRecord(value) &&
    typeof value.savedAt === 'number' &&
    Number.isFinite(value.savedAt) &&
    now - value.savedAt <= MAX_AGE_MS &&
    Array.isArray(value.recents) &&
    value.recents.length <= MAX_RECENTS &&
    value.recents.every(isSessionInfo) &&
    Array.isArray(value.cron) &&
    value.cron.length <= MAX_CRON &&
    value.cron.every(isSessionInfo) &&
    Array.isArray(value.messaging) &&
    value.messaging.length <= MAX_MESSAGING &&
    value.messaging.every(isSessionInfo) &&
    typeof value.messagingTruncated === 'boolean' &&
    isBooleanRecord(value.profilesTruncated) &&
    isUsageRecord(value.profilesUsage)
  )
}

function readCache(): SessionListCache {
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
        .filter((entry): entry is [string, StoredSessionListSnapshot] => isStoredSnapshot(entry[1], now))
        .sort((left, right) => right[1].savedAt - left[1].savedAt)
        .slice(0, MAX_CONTEXTS)
    )
  } catch {
    return {}
  }
}

export function sessionListCacheScopeKey(scope: SessionListCacheScope): string {
  return JSON.stringify([scope.connectionId?.trim() || 'local', scope.profile.trim() || 'default'])
}

export function readSessionListSnapshot(scope: SessionListCacheScope): SessionListSnapshot | null {
  const cached = readCache()[sessionListCacheScopeKey(scope)]

  if (!cached) {
    return null
  }

  const { savedAt: _savedAt, ...snapshot } = cached

  return snapshot
}

export function writeSessionListSnapshot(scope: SessionListCacheScope, snapshot: SessionListSnapshot): void {
  const key = sessionListCacheScopeKey(scope)
  const cache = readCache()

  cache[key] = {
    cron: snapshot.cron.slice(0, MAX_CRON),
    messaging: snapshot.messaging.slice(0, MAX_MESSAGING),
    messagingTruncated: snapshot.messagingTruncated,
    profilesTruncated: snapshot.profilesTruncated,
    profilesUsage: snapshot.profilesUsage,
    recents: snapshot.recents.slice(0, MAX_RECENTS),
    savedAt: Date.now()
  }

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

export const SESSION_LIST_CACHE_STORAGE_KEY = STORAGE_KEY
