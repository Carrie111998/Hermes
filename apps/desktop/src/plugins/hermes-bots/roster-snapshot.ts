/**
 * Bounded, presentation-only Bot roster persistence.
 *
 * The live gateway remains authoritative. These helpers deliberately exclude
 * session identity, previews, activity, prompts and ui_meta so restoring a
 * snapshot can paint rows but can never choose or create a chat.
 */

import type { GatewaySource, RosterRow } from './types'

export const ROSTER_SNAPSHOT_KEY = 'roster-snapshot-v1'
export const ROSTER_SNAPSHOT_MAX_CONNECTIONS = 8
export const ROSTER_SNAPSHOT_MAX_ROWS = 256

export interface PersistedRosterEntry {
  fetchedAt: number
  profiles: RosterRow[]
  sources: GatewaySource[]
}

export interface PersistedRosterSnapshot {
  entries: Record<string, PersistedRosterEntry>
  version: 1
}

export const EMPTY_ROSTER_SNAPSHOT: PersistedRosterSnapshot = {
  entries: {},
  version: 1
}

function boundedText(value: unknown, limit = 512): string | undefined {
  return typeof value === 'string' ? value.slice(0, limit) : undefined
}

/** Keep only the fields needed to paint and route one roster row. */
export function compactRosterProfile(value: unknown): RosterRow | null {
  const profile = value && typeof value === 'object' ? (value as Record<string, unknown>) : null
  const name = boundedText(profile?.name, 64)?.trim()

  if (!profile || !name) {
    return null
  }

  const displayName = boundedText(profile.display_name, 160)
  const description = boundedText(profile.description, 1024)
  const connectionId = boundedText(profile.connectionId, 160)
  const connectionLabel = boundedText(profile.connectionLabel, 160)
  const connectionKind = boundedText(profile.connectionKind, 32)
  const targetProfile = boundedText(profile.targetProfile, 64)
  const handle = boundedText(profile.handle, 160)
  const title = boundedText(profile.title, 160)
  const sourceError = boundedText(profile.sourceError, 512)

  const compact: RosterRow = {
    name,
    ...(displayName !== undefined ? { display_name: displayName } : {}),
    ...(description !== undefined ? { description } : {}),
    ...(connectionId !== undefined ? { connectionId } : {}),
    ...(connectionLabel !== undefined ? { connectionLabel } : {}),
    ...(connectionKind !== undefined ? { connectionKind } : {}),
    ...(targetProfile !== undefined ? { targetProfile } : {}),
    ...(handle !== undefined ? { handle } : {}),
    ...(title !== undefined ? { title } : {}),
    ...(sourceError !== undefined ? { sourceError } : {})
  }

  for (const key of ['has_avatar', 'sourceScoped', 'remoteSource', 'sourceMissing', 'sourceReachable'] as const) {
    if (typeof profile[key] === 'boolean') {
      compact[key] = profile[key]
    }
  }

  const route = profile.route && typeof profile.route === 'object' ? (profile.route as Record<string, unknown>) : null
  const routeConnectionId = boundedText(route?.connectionId, 160)?.trim()
  const routeProfile = boundedText(route?.profile, 64)?.trim()

  if (route && routeConnectionId && routeProfile) {
    compact.route = {
      connectionId: routeConnectionId,
      mode: route.mode === 'local' ? 'local' : 'remote',
      profile: routeProfile,
      targetProfile: boundedText(route.targetProfile, 64)?.trim() || routeProfile
    }
  }

  return compact
}

export function compactRosterSource(value: unknown): GatewaySource | null {
  const source = value && typeof value === 'object' ? (value as Record<string, unknown>) : null
  const connectionId = boundedText(source?.connectionId, 160)?.trim()

  if (!source || !connectionId) {
    return null
  }

  const error = boundedText(source.error, 512)

  return {
    connectionId,
    kind: boundedText(source.kind, 32) || 'remote',
    label: boundedText(source.label, 160) || connectionId,
    ...(typeof source.reachable === 'boolean' ? { reachable: source.reachable } : {}),
    ...(error ? { error } : {})
  }
}

export function normalizeRosterSnapshot(value: unknown): PersistedRosterSnapshot {
  const root = value && typeof value === 'object' ? (value as Record<string, unknown>) : null
  const rawEntries = root?.version === 1 && root.entries && typeof root.entries === 'object' ? root.entries : {}
  const entries: Record<string, PersistedRosterEntry> = {}

  for (const [connectionId, rawEntry] of Object.entries(rawEntries).slice(-ROSTER_SNAPSHOT_MAX_CONNECTIONS)) {
    const entry = rawEntry && typeof rawEntry === 'object' ? (rawEntry as Record<string, unknown>) : null

    const profiles = (Array.isArray(entry?.profiles) ? entry.profiles : [])
      .slice(0, ROSTER_SNAPSHOT_MAX_ROWS)
      .map(compactRosterProfile)
      .filter((profile): profile is RosterRow => profile !== null)

    const sources = (Array.isArray(entry?.sources) ? entry.sources : [])
      .slice(0, ROSTER_SNAPSHOT_MAX_CONNECTIONS)
      .map(compactRosterSource)
      .filter((source): source is GatewaySource => source !== null)

    if (profiles.length) {
      entries[String(connectionId)] = {
        fetchedAt: Math.max(0, Number(entry?.fetchedAt || 0)),
        profiles,
        sources
      }
    }
  }

  return { entries, version: 1 }
}

export function updateRosterSnapshot(
  snapshot: PersistedRosterSnapshot,
  connectionId: unknown,
  profiles: unknown,
  sources: unknown,
  fetchedAt: unknown
): PersistedRosterSnapshot {
  const key = String(connectionId || 'local').trim() || 'local'

  const compactProfiles = (Array.isArray(profiles) ? profiles : [])
    .slice(0, ROSTER_SNAPSHOT_MAX_ROWS)
    .map(compactRosterProfile)
    .filter((profile): profile is RosterRow => profile !== null)

  if (!compactProfiles.length) {
    return snapshot
  }

  const compactSources = (Array.isArray(sources) ? sources : [])
    .slice(0, ROSTER_SNAPSHOT_MAX_CONNECTIONS)
    .map(compactRosterSource)
    .filter((source): source is GatewaySource => source !== null)

  const entries = {
    ...snapshot.entries,
    [key]: {
      fetchedAt: Math.max(0, Number(fetchedAt || Date.now())),
      profiles: compactProfiles,
      sources: compactSources
    }
  }

  const newest = Object.entries(entries)
    .sort(([, left], [, right]) => left.fetchedAt - right.fetchedAt)
    .slice(-ROSTER_SNAPSHOT_MAX_CONNECTIONS)

  return { entries: Object.fromEntries(newest), version: 1 }
}
