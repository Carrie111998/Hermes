/**
 * Canonical registry route identity for Desktop — Magnum #94724 Phase 1.
 *
 * Identity is frozen before dialing, while the complete auth/transport
 * envelope still exists. Compatibility callers may reuse the same contract,
 * but must never reconstruct a stronger identity from post-dial metadata.
 *
 * Phase 1 adds:
 *  - branded ConnectionId / ProfileKey (opaque strings)
 *  - generation-bound RouteKey (connectionId + generation + desktopProfile/targetProfile)
 *  - discriminated ResolvedRoute (registered vs legacy-unregistered)
 *  - helpers to mint and validate RouteKey without weakening it downstream
 */

import { normalizeRemoteBaseUrl, normalizeRemoteHeaders, normalizeSshConfig, normAuthMode } from './connection-config'
import type { ConnectionRegistry, RegistryConnection } from './connection-registry'

// ── Branded ids — makes cross-route aliasing a type error ──────────────────
export type ConnectionId = string & { readonly __brand: 'ConnectionId' }
export type ProfileKey = string & { readonly __brand: 'ProfileKey' }

export function asConnectionId(id: string): ConnectionId {
  return String(id).trim() as ConnectionId
}

export function asProfileKey(profile: string): ProfileKey {
  return (String(profile || '').trim() || 'default') as ProfileKey
}

export function normalizeProfileKeyBranded(profile: unknown): ProfileKey {
  return asProfileKey(String(profile ?? ''))
}

// ── RouteKey — immutable, generation-bound (§3.1) ──────────────────────────
export type RouteKey = Readonly<{
  connectionId: ConnectionId
  generation: number
  desktopProfile: ProfileKey
  targetProfile: ProfileKey
}>

// ── ResolvedRoute — registered identity is sticky, legacy is fenceable (§3.1, §11)
export type RegisteredRoute = Readonly<{
  kind: 'registered'
  key: RouteKey
}>
export type LegacyRouteIntent = Readonly<{
  kind: 'legacy'
  descriptor: StoredRoute
  provenance: 'environment' | 'profile-v1' | 'settings-v1' | 'unknown'
}>
export type ResolvedRoute = LegacyRouteIntent | RegisteredRoute
export type UnresolvedRoute = Readonly<{ kind: 'unresolved'; reason: string }>

export function makeRouteKey(connection: RegistryConnection, desktopProfile: string): RouteKey {
  const generation = Number.isInteger((connection as { generation?: unknown }).generation)
    ? (connection as { generation: number }).generation
    : 1

  const desktop = asProfileKey(desktopProfile)

  // SSH remotes may map desktopProfile → remoteProfile (target namespace)
  const targetRaw =
    connection.kind === 'ssh' && String(connection.remoteProfile || '').trim()
      ? String(connection.remoteProfile).trim()
      : String(desktopProfile || '').trim() || 'default'

  return {
    connectionId: asConnectionId(connection.id),
    generation,
    desktopProfile: desktop,
    targetProfile: asProfileKey(targetRaw)
  } as RouteKey
}

export function isRouteKeyCurrent(registry: ConnectionRegistry, key: RouteKey): boolean {
  const stored = registry.connections.find(c => c.id === (key.connectionId as string))

  if (!stored) {
    return false
  }

  const gen = Number.isInteger((stored as { generation?: unknown }).generation)
    ? (stored as { generation: number }).generation
    : 1

  return gen === key.generation
}

/**
 * Does a dialed descriptor belong to the profile scope its route asked for?
 *
 * Deliberately permissive about the legitimate re-scopings: a shared-primary
 * descriptor advertises the primary's `descriptorProfile` rather than the
 * requested key, and an SSH route maps desktopProfile → remoteProfile (this
 * RouteKey's `targetProfile`). A descriptor carrying no profile of its own
 * simply inherits the one that was requested. What this rejects is a descriptor
 * scoped to some THIRD profile — which would mean a dial handed back another
 * route's backend.
 */
export function descriptorScopeMatchesRoute(
  descriptor: null | undefined | { profile?: unknown; sharedPrimary?: unknown },
  key: RouteKey
): boolean {
  const advertised = String(descriptor?.profile ?? '').trim()

  if (!advertised || descriptor?.sharedPrimary) {
    return true
  }

  return advertised === String(key.desktopProfile) || advertised === String(key.targetProfile)
}

export function routeKeyScopeKey(key: RouteKey): string {
  // Reuses the same composite language as backendScopeKey for pool/socket maps.
  // Deliberately generation-free: pool slots are per (connection, profile), and
  // a generation bump re-dials into the SAME slot rather than leaking a new one.
  const conn = key.connectionId as string
  const profile = key.desktopProfile as string

  if (!conn || conn === 'local') {
    return profile
  }

  return `conn:${conn}::${profile}`
}

/**
 * Partition key — unlike the pool key, this IS generation-bound. Partitioned
 * stores hold data belonging to a specific gateway authority; a generation bump
 * is a new authority, so it must get a fresh partition instead of inheriting
 * the previous gateway's data (and its now-stale `partition.route`).
 */
export function routeKeyPartitionKey(key: RouteKey): string {
  return `${routeKeyScopeKey(key)}@${key.generation}`
}

interface SshRouteConfig {
  host: string
  keyPath?: string
  mode: 'ssh'
  port?: number
  remoteHermesPath?: string
  remoteProfile?: string
  user?: string
}

export type StoredRoute =
  | {
      authMode?: unknown
      headers?: Record<string, unknown>
      kind: 'cloud' | 'remote'
      org?: unknown
      token?: unknown
      url?: unknown
    }
  | ({ kind: 'ssh' } & Partial<SshRouteConfig>)

function stableValue(value: unknown): string {
  if (!value || typeof value !== 'object') {
    return JSON.stringify(value ?? null)
  }

  if (Array.isArray(value)) {
    return `[${value.map(stableValue).join(',')}]`
  }

  return `{${Object.entries(value)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, item]) => `${JSON.stringify(key)}:${stableValue(item)}`)
    .join(',')}}`
}

function canonicalHeaders(headers: unknown): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(normalizeRemoteHeaders(headers))
      .map(([name, value]): [string, unknown] => [name.toLowerCase(), value])
      .sort(([left], [right]) => left.localeCompare(right))
  )
}

function routeIdentity(route: StoredRoute): null | string {
  if (route.kind === 'ssh') {
    const ssh = normalizeSshConfig({ ...route, mode: 'ssh' })

    if (!ssh) {
      return null
    }

    return stableValue({
      host: ssh.host.trim().toLowerCase(),
      keyPath: ssh.keyPath || '',
      kind: 'ssh',
      port: ssh.port || 22,
      remoteHermesPath: ssh.remoteHermesPath || '',
      remoteProfile: ssh.remoteProfile || '',
      user: (ssh.user || '').trim().toLowerCase()
    })
  }

  try {
    const authMode = normAuthMode(route.authMode)

    return stableValue({
      authMode,
      headers: canonicalHeaders(route.headers),
      kind: route.kind,
      org: route.kind === 'cloud' ? String(route.org || '').trim() : '',
      token: authMode === 'token' ? (route.token ?? null) : null,
      url: normalizeRemoteBaseUrl(route.url)
    })
  } catch {
    return null
  }
}

function registryRoute(connection: RegistryConnection): null | StoredRoute {
  if (connection.kind === 'local') {
    return null
  }

  return connection as StoredRoute
}

/**
 * Match the complete pre-dial route identity used by #88922.
 *
 * `primary` accepts only the configured primary when its full envelope is
 * equal. `unique` accepts exactly one full-envelope match. Zero and multiple
 * matches deliberately remain unresolved.
 */
export function matchingConnectionId(
  registry: ConnectionRegistry,
  route: StoredRoute,
  strategy: 'primary' | 'unique'
): undefined | string {
  const identity = routeIdentity(route)

  if (!identity) {
    return undefined
  }

  if (strategy === 'primary') {
    const primary = registry.connections.find(connection => connection.id === registry.primary)
    const candidate = primary && registryRoute(primary)

    return candidate && routeIdentity(candidate) === identity ? primary.id : undefined
  }

  const matches = registry.connections.filter(connection => {
    const candidate = registryRoute(connection)

    return candidate ? routeIdentity(candidate) === identity : false
  })

  return matches.length === 1 ? matches[0].id : undefined
}
