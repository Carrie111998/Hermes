/**
 * resource-ref.ts — Magnum #94724 §9 §11
 *
 * Resources carry ownership. Foreground never provides ownership.
 *
 *   const ref: ResourceRef<SessionId> = { owner: routeKey, id: sessionId }
 *   const client = supervisor.bind(ref.owner)
 *   await client.sessions.archive(ref.id)
 *
 * Ambient routing (hermesApi / getCurrentGateway / activeGateway) is banned
 * for resource-bound operations; use ResourceRef or RouteClient.
 */

import type { RouteKey } from '../../electron/connection-route-identity'

// Branded resource ids (opaque)
export type SessionId = string & { readonly __brand: 'SessionId' }
export type CronJobId = string & { readonly __brand: 'CronJobId' }
export type ProjectId = string & { readonly __brand: 'ProjectId' }
export type ArtifactId = string & { readonly __brand: 'ArtifactId' }

export function asSessionId(id: string): SessionId { return String(id).trim() as SessionId }

export function asCronJobId(id: string): CronJobId { return String(id).trim() as CronJobId }

export type ResourceRef<T> = Readonly<{
  owner: RouteKey
  id: T
}>

export function resourceRef<T>(owner: RouteKey, id: T): ResourceRef<T> {
  return { owner, id }
}

// Explicit exceptional API for genuinely foreground-global actions (§10)
// Naming makes ambient use grep-able. Prefer ResourceRef for everything else.
export type ForegroundApiTag = 'foreground'

/**
 * Tag a call-site as intentionally foreground-scoped.
 * Code review can grep for `foregroundApi` — the only legitimate ambient surface.
 */
export function foregroundApi<T>(fn: () => T): T {
  return fn()
}

// Lint helper: list of banned ambient call-site patterns (§9)
// Keep in sync with docs. A future eslint rule can enforce: no direct
// hermesApi()/getCurrentGateway()/activeGateway in resource-bound modules.
export const BANNED_AMBIENT_PATTERNS = [
  'hermesApi()',
  'getCurrentGateway()',
  'activeGateway',
  'selectedConnection',
  'currentProfile',
] as const
