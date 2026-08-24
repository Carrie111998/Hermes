import {
  evaluateRuntimeReadiness,
  type RuntimeReadinessRequester,
  type RuntimeReadinessResult
} from './runtime-readiness'

export interface ProfileConnectionRow {
  id: string
  kind?: string
  label?: string
}

export interface ProfileRouteOwner {
  connectionId: string
  connectionKind: 'local' | 'remote'
  connectionLabel: string
  name: string
  remoteSource: boolean
  route: Readonly<{
    connectionId: string
    mode: 'local' | 'remote'
    profile: string
    targetProfile: string
  }>
  sourceScoped: true
}

export interface ProfileRosterRow {
  connectionId?: string
  name?: string
  remoteSource?: boolean
  targetProfile?: string
}

export function buildProfileRouteOwner({
  activeConnectionId,
  connections,
  name,
  targetConnection
}: {
  activeConnectionId?: string
  connections?: ProfileConnectionRow[] | null
  name: string
  targetConnection?: string
}): ProfileRouteOwner {
  const connectionId = String(targetConnection || activeConnectionId || 'local').trim() || 'local'
  const row = (Array.isArray(connections) ? connections : []).find(connection => connection.id === connectionId)
  const mode = row?.kind === 'local' || connectionId === 'local' ? 'local' : 'remote'

  return {
    name,
    connectionId,
    connectionKind: mode,
    connectionLabel: row?.label || (connectionId === 'local' ? 'This device' : connectionId),
    sourceScoped: true,
    remoteSource: mode === 'remote',
    route: Object.freeze({ connectionId, mode, profile: name, targetProfile: name })
  }
}

/** Return clone sources owned by one connection. The default profile is always
 * available even before that connection's roster finishes hydrating. */
export function profileNamesForConnection(
  roster: ProfileRosterRow[] | null | undefined,
  connectionId: string,
  remoteTarget: boolean
): string[] {
  const names = (Array.isArray(roster) ? roster : [])
    .filter(bot =>
      remoteTarget
        ? bot.connectionId === connectionId
        : !bot.remoteSource && (!connectionId || !bot.connectionId || bot.connectionId === connectionId)
    )
    .map(bot => String(bot.targetProfile || bot.name || '').trim())
    .filter(Boolean)

  return [...new Set(['default', ...names])]
}

export async function evaluateProfileReadiness({
  evaluate = evaluateRuntimeReadiness,
  label,
  owner,
  requestedProvider,
  request
}: {
  evaluate?: typeof evaluateRuntimeReadiness
  label?: string
  owner: ProfileRouteOwner
  requestedProvider?: string
  request: (owner: ProfileRouteOwner, method: string, params?: Record<string, unknown>) => Promise<unknown>
}): Promise<RuntimeReadinessResult> {
  if (typeof evaluate !== 'function') {
    return { checksDisagree: false, ready: true, reason: null, source: 'fallback' }
  }

  const requester: RuntimeReadinessRequester = <T = unknown>(method: string, params?: Record<string, unknown>) =>
    request(owner, method, params) as Promise<T>

  return evaluate(requester, {
    requestedProvider: requestedProvider?.trim() || undefined,
    defaultReason: `Configure a provider on ${label || owner.connectionLabel} before starting this Bot's chat.`
  })
}
