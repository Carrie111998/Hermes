/**
 * persisted-state.ts — Magnum #94724 §14
 *
 * Every persisted item declares its scope. Storage keys incorporate the
 * appropriate identifiers so route-scoped state never leaks between gateways.
 */

export type StateScope = 'global' | 'connection' | 'route' | 'session'

export interface PersistedStateOpts {
  name: string
  scope: StateScope
  version: number
}

export function definePersistedState(opts: PersistedStateOpts) {
  return {
    ...opts,
    storageKey(parts: { connectionId?: string; routeKey?: string; sessionId?: string }): string {
      const base = `hermes:${opts.name}:v${opts.version}`

      switch (opts.scope) {
        case 'global':
          return base

        case 'connection':
          if (!parts.connectionId) {throw new Error(`Persisted state "${opts.name}" (scope=connection) requires connectionId`)}

          return `${base}:conn:${parts.connectionId}`

        case 'route':
          if (!parts.routeKey) {throw new Error(`Persisted state "${opts.name}" (scope=route) requires routeKey`)}

          return `${base}:route:${parts.routeKey}`

        case 'session':
          if (!parts.sessionId) {throw new Error(`Persisted state "${opts.name}" (scope=session) requires sessionId`)}

          return `${base}:session:${parts.sessionId}`

        default:
          return base
      }
    },
  }
}

// Audit helper: direct persistence calls that bypass scope declaration.
// A future lint rule can forbid raw localStorage/electron-store outside this module.
export const DIRECT_PERSISTENCE_BANNED = [
  'localStorage.setItem',
  'localStorage.getItem',
  'electron-store',
] as const
