/**
 * Pure route planner for Desktop's legacy v1 connection config. It preserves
 * v2 registry provenance before URL normalization, OAuth, or SSH tunnelling
 * discard dial details. Environment routes deliberately remain unregistered.
 */

import crypto from 'node:crypto'

import {
  connectionScopeKey,
  modeIsRemoteLike,
  normalizeRemoteBaseUrl,
  normalizeRemoteHeaders,
  normalizeSshConfig,
  normAuthMode,
  profileRemoteOverride,
  profileSshOverride
} from './connection-config'
import type { ConnectionRegistry, RegistryConnection } from './connection-registry'

type RouteSource = 'env' | 'profile' | 'settings'

interface SshRouteConfig {
  host: string
  keyPath?: string
  mode: 'ssh'
  port?: number
  remoteHermesPath?: string
  remoteProfile?: string
  user?: string
}

export type DesktopRemoteRoute =
  | {
      authMode: 'oauth' | 'token'
      connectionId?: string
      headers?: Record<string, unknown>
      kind: 'cloud' | 'remote'
      org?: string
      source: RouteSource
      token?: unknown
      url: string
    }
  | {
      connectionId?: string
      kind: 'ssh'
      source: Exclude<RouteSource, 'env'>
      ssh: SshRouteConfig
      token?: unknown
    }

export interface DesktopRemoteRouteInput {
  config: Record<string, any>
  env?: { token?: null | string; url?: null | string }
  profile?: null | string
  registry: ConnectionRegistry
}

export interface RegistryBackendTarget {
  connectionId: string
  profile: string
}

/**
 * An exact v2 registry identity is authoritative for backend ownership.
 * Returning it here lets the primary v1-compatible route delegate to the
 * registry pool instead of opening the same SSH gateway a second time under
 * the legacy global scope — two scopes meant two ownership ids, so one gateway
 * spawned two identical `hermes serve --isolated` backends for one profile.
 *
 * `connectionId` is only ever set by resolveDesktopRemoteRoute() when the
 * stored v1 route matches ONE registry entry byte-for-byte (host, user, port,
 * key path, remote Hermes path, remote profile — or URL/auth/headers/org for
 * gateway routes). An ambiguous or partial match leaves it undefined and this
 * returns null, so the caller keeps the historical v1 path.
 */
export function registryTargetForRoute(
  route: DesktopRemoteRoute | null,
  profile?: null | string
): null | RegistryBackendTarget {
  const connectionId = String(route?.connectionId || '').trim()

  if (!connectionId) {
    return null
  }

  return {
    connectionId,
    profile: String(profile ?? '').trim() || 'default'
  }
}

/**
 * Digest of one `ssh -G` dump, ignoring the echoed `host <alias>` line.
 *
 * Two routes that resolve to the same effective configuration dial the SAME
 * machine even when they are spelled differently - typically an ~/.ssh/config
 * alias on one side and the resolved host/user on the other. Everything that
 * decides WHERE and AS WHOM ssh connects (hostname, user, port, identityfile,
 * proxyjump, ...) stays in the digest; only the alias the caller typed is
 * dropped, because that is spelling, not destination.
 *
 * `hostname <resolved>` is deliberately NOT filtered: the pattern requires
 * whitespace directly after `host`.
 */
export function effectiveDialDigest(sshConfigDump: string): string {
  const lines = String(sshConfigDump || '')
    .split(/\r?\n/)
    .map(line => line.trim())
    .filter(line => line && !/^host\s/i.test(line))

  return crypto.createHash('sha256').update(lines.join('\n')).digest('hex')
}

/**
 * Fields that `ssh -G` cannot answer for: they describe what Hermes does ONCE
 * connected, not how to connect. Two routes may only be treated as one gateway
 * when these agree exactly.
 */
export function sshPayloadFieldsMatch(left: any, right: any): boolean {
  return (
    String(left?.remoteHermesPath || '') === String(right?.remoteHermesPath || '') &&
    String(left?.remoteProfile || '') === String(right?.remoteProfile || '')
  )
}

type StoredRoute =
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
      host: ssh.host,
      keyPath: ssh.keyPath || '',
      kind: 'ssh',
      port: ssh.port || 22,
      remoteHermesPath: ssh.remoteHermesPath || '',
      remoteProfile: ssh.remoteProfile || '',
      user: ssh.user || ''
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

function matchingConnectionId(
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

function withConnectionId<T extends object>(route: T, connectionId?: string): T & { connectionId?: string } {
  return connectionId ? { ...route, connectionId } : route
}

/**
 * Select one remote route with the existing precedence and freeze any exact
 * registry identity before I/O. A null result means the profile resolves
 * locally. Invalid dial data remains the dialler's error, except the existing
 * env-pair validation which belongs to selection.
 */
export function resolveDesktopRemoteRoute({
  config,
  env = {},
  profile,
  registry
}: DesktopRemoteRouteInput): DesktopRemoteRoute | null {
  const profileKey = connectionScopeKey(profile)
  const profileConfig = profileKey ? config.profiles?.[profileKey] : null
  const sshOverride = profileSshOverride(config, profile)

  if (sshOverride) {
    const route = { ...sshOverride, kind: 'ssh' as const }

    return withConnectionId(
      {
        kind: 'ssh' as const,
        source: 'profile' as const,
        ssh: sshOverride,
        token: profileConfig?.token
      },
      matchingConnectionId(registry, route, 'unique')
    )
  }

  const override = profileRemoteOverride(config, profile)

  if (override) {
    const kind = profileConfig?.mode === 'cloud' ? 'cloud' : 'remote'
    const authMode = override.authMode === 'oauth' ? 'oauth' : 'token'
    const route = { ...profileConfig, kind } as StoredRoute

    return withConnectionId(
      {
        authMode,
        headers: override.headers,
        kind,
        org: kind === 'cloud' ? String(profileConfig?.org || '').trim() || undefined : undefined,
        source: 'profile' as const,
        token: override.token,
        url: override.url
      },
      matchingConnectionId(registry, route, 'unique')
    )
  }

  const envUrl = String(env.url || '').trim()

  if (envUrl) {
    const envToken = String(env.token || '').trim()

    if (!envToken) {
      throw new Error(
        'HERMES_DESKTOP_REMOTE_URL is set but HERMES_DESKTOP_REMOTE_TOKEN is not. ' +
          'Both must be provided to connect to a remote Hermes backend.'
      )
    }

    return { authMode: 'token', kind: 'remote', source: 'env', token: envToken, url: envUrl }
  }

  if (config.mode === 'ssh') {
    const ssh = normalizeSshConfig({ mode: 'ssh', ...(config.remote || {}) })

    if (!ssh) {
      throw new Error('SSH remote mode is selected but no host is configured.')
    }

    const route = { ...ssh, kind: 'ssh' as const }

    return withConnectionId(
      { kind: 'ssh' as const, source: 'settings' as const, ssh, token: config.remote?.token },
      matchingConnectionId(registry, route, 'primary')
    )
  }

  if (!modeIsRemoteLike(config.mode)) {
    return null
  }

  const kind = config.mode === 'cloud' ? 'cloud' : 'remote'
  const authMode = normAuthMode(config.remote?.authMode)
  const route = { ...config.remote, kind } as StoredRoute

  return withConnectionId(
    {
      authMode,
      headers: config.remote?.headers,
      kind,
      org: kind === 'cloud' ? String(config.remote?.org || '').trim() || undefined : undefined,
      source: 'settings' as const,
      token: config.remote?.token,
      url: String(config.remote?.url || '')
    },
    matchingConnectionId(registry, route, 'primary')
  )
}
