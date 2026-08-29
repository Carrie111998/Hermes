import {
  authMcpServer,
  cancelMcpOAuthFlow,
  getApiRequestConnection,
  getMcpOAuthFlow,
  type ProfileScope
} from '@/hermes'
import { requestGatewayForAgent, requestGatewayForProfile } from '@/store/gateway'

import { completeMcpDesktopOAuth, type McpOAuthFlow } from './mcp-dashboard-oauth'

interface RoutedOptions {
  serverName: string
  scope?: ProfileScope
  openExternal: (url: string) => Promise<void>
  cancelled?: () => boolean
  sleep?: (milliseconds: number) => Promise<void>
  maxPollFailures?: number
}

interface LocalStartResult {
  session_id: string
  auth_url: string
}

interface LocalPollResult {
  status: 'pending' | 'approved' | 'error'
  error_message?: string | null
  auth_url?: string | null
  tools?: McpOAuthFlow['tools']
}

interface RoutingDependencies {
  connectionMode: (scope?: ProfileScope) => Promise<'local' | 'remote'>
  requestRpc: <T>(scope: ProfileScope | undefined, method: string, params: Record<string, unknown>) => Promise<T>
  restStart: (name: string, scope?: ProfileScope) => Promise<McpOAuthFlow>
  restStatus: (flowId: string, scope?: ProfileScope) => Promise<McpOAuthFlow>
  restCancel: (flowId: string, scope?: ProfileScope) => Promise<unknown>
}

function profileFromScope(scope?: ProfileScope): string {
  if (scope && typeof scope === 'object') {
    return (scope.profile ?? '').trim() || 'default'
  }

  return (scope ?? '').trim() || 'default'
}

interface ResolvedMcpOAuthScope {
  connectionId: null | string
  profile: string
}

export function resolveMcpOAuthScope(
  scope: ProfileScope | undefined,
  ambientConnectionId: null | string
): ResolvedMcpOAuthScope {
  const profile = profileFromScope(scope)

  if (scope && typeof scope === 'object') {
    const requested = (scope.connectionId ?? '').trim()

    return {
      connectionId: requested && requested !== 'local' ? requested : 'local',
      profile
    }
  }

  return {
    connectionId: (ambientConnectionId ?? '').trim() || null,
    profile
  }
}

function inferredConnectionMode(connection: { baseUrl: string; mode?: 'local' | 'remote' }): 'local' | 'remote' {
  if (connection.mode) {
    return connection.mode
  }

  try {
    const host = new URL(connection.baseUrl).hostname

    return host === '127.0.0.1' || host === 'localhost' || host === '::1' ? 'local' : 'remote'
  } catch {
    return 'remote'
  }
}

async function connectionMode(scope?: ProfileScope): Promise<'local' | 'remote'> {
  const resolved = resolveMcpOAuthScope(scope, getApiRequestConnection())

  const connection =
    resolved.connectionId && window.hermesDesktop.getConnectionFor
      ? await window.hermesDesktop.getConnectionFor(resolved)
      : await window.hermesDesktop.getConnection(resolved.profile)

  return inferredConnectionMode(connection)
}

async function requestRpc<T>(
  scope: ProfileScope | undefined,
  method: string,
  params: Record<string, unknown>
): Promise<T> {
  const resolved = resolveMcpOAuthScope(scope, getApiRequestConnection())

  if (resolved.connectionId) {
    return requestGatewayForAgent<T>(resolved.connectionId, resolved.profile, method, params)
  }

  return requestGatewayForProfile<T>(resolved.profile, method, params)
}

const defaultDependencies: RoutingDependencies = {
  connectionMode,
  requestRpc,
  restStart: (name, scope) => authMcpServer(name, scope),
  restStatus: (flowId, scope) => getMcpOAuthFlow(flowId, scope),
  restCancel: (flowId, scope) => cancelMcpOAuthFlow(flowId, scope)
}

function localFlow(serverName: string, started: LocalStartResult): McpOAuthFlow {
  return {
    flow_id: started.session_id,
    server_name: serverName,
    status: 'authorization_required',
    authorization_url: started.auth_url,
    error: null
  }
}

function localPoll(serverName: string, flowId: string, current: LocalPollResult): McpOAuthFlow {
  return {
    flow_id: flowId,
    server_name: serverName,
    status: current.status === 'pending' ? 'authorization_required' : current.status,
    authorization_url: current.auth_url ?? null,
    error: current.error_message ?? null,
    tools: current.tools
  }
}

export async function completeRoutedMcpDesktopOAuth(
  options: RoutedOptions,
  dependencies: RoutingDependencies = defaultDependencies
): Promise<McpOAuthFlow> {
  const { serverName, scope } = options
  const resolved = resolveMcpOAuthScope(scope, getApiRequestConnection())

  const frozenScope: ProfileScope = {
    connectionId: resolved.connectionId ?? 'local',
    profile: resolved.profile
  }

  const mode = await dependencies.connectionMode(frozenScope)

  if (mode === 'remote') {
    return completeMcpDesktopOAuth({
      ...options,
      start: name => dependencies.restStart(name, frozenScope),
      status: flowId => dependencies.restStatus(flowId, frozenScope),
      cancel: flowId => dependencies.restCancel(flowId, frozenScope)
    })
  }

  return completeMcpDesktopOAuth({
    ...options,
    start: async name =>
      localFlow(
        name,
        await dependencies.requestRpc<LocalStartResult>(frozenScope, 'mcp.servers.oauth.start', { name })
      ),
    status: async flowId =>
      localPoll(
        serverName,
        flowId,
        await dependencies.requestRpc<LocalPollResult>(frozenScope, 'mcp.servers.oauth.poll', {
          name: serverName,
          session_id: flowId
        })
      ),
    cancel: flowId =>
      dependencies.requestRpc(frozenScope, 'mcp.servers.oauth.cancel', {
        name: serverName,
        session_id: flowId
      })
  })
}
