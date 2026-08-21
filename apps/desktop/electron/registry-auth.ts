import { isGatewayAuthRejection } from './connection-config'

const DURABLE_SCOPE_RE = /^(?!draft-)[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/
const DRAFT_SCOPE_RE = /^draft-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i

export type RegistryAuthFailureKind = 'auth-required' | 'transport-error'

export interface RegistryAuthFailure {
  error: string
  kind: RegistryAuthFailureKind
  ok: false
}

export function validateRegistryAuthScope(rawScope: unknown): string {
  const scope = String(rawScope ?? '').trim()

  if (scope === 'local' || (!DURABLE_SCOPE_RE.test(scope) && !DRAFT_SCOPE_RE.test(scope))) {
    throw new Error('A valid registered gateway authentication scope is required.')
  }

  return scope
}

export function createDraftRegistryAuthScope(randomUuid: () => string = () => crypto.randomUUID()): string {
  return validateRegistryAuthScope(`draft-${randomUuid()}`)
}

export function registryAuthPartition(scope: unknown): string {
  return `persist:hermes-registry-auth-${validateRegistryAuthScope(scope)}`
}

export function registryAuthStorageKey(scope: unknown, _baseUrl: unknown): string {
  return `registry:${validateRegistryAuthScope(scope)}`
}

export async function mintRegistryAuthTicket(
  rawScope: unknown,
  baseUrl: string,
  headers: Record<string, string>,
  mintTicket: (baseUrl: string, headers: Record<string, string>, scope: string) => Promise<string>
): Promise<string> {
  return mintTicket(baseUrl, headers, validateRegistryAuthScope(rawScope))
}

interface RegistryAuthCleanupDependencies {
  clearCookies: (scope: string, baseUrl: string) => Promise<unknown>
  clearNativeTokens: (storageKey: string) => unknown
}

interface RegistryAuthPromotionDependencies extends RegistryAuthCleanupDependencies {
  copyCookies: (fromScope: string, toScope: string, baseUrl: string) => Promise<unknown>
  moveNativeTokens: (fromStorageKey: string, toStorageKey: string) => unknown
}

export async function clearRegistryAuthScope(
  rawScope: unknown,
  baseUrl: string,
  deps: RegistryAuthCleanupDependencies
): Promise<void> {
  const scope = validateRegistryAuthScope(rawScope)
  await deps.clearCookies(scope, baseUrl)
  deps.clearNativeTokens(registryAuthStorageKey(scope, baseUrl))
}

export async function promoteRegistryAuthScope(
  rawFromScope: unknown,
  rawToScope: unknown,
  baseUrl: string,
  deps: RegistryAuthPromotionDependencies
): Promise<void> {
  const fromScope = validateRegistryAuthScope(rawFromScope)
  const toScope = validateRegistryAuthScope(rawToScope)
  await deps.copyCookies(fromScope, toScope, baseUrl)
  deps.moveNativeTokens(registryAuthStorageKey(fromScope, baseUrl), registryAuthStorageKey(toScope, baseUrl))
  await clearRegistryAuthScope(fromScope, baseUrl, deps)
}

export interface RegistryAuthPromotionSaveOptions<T> {
  baseUrl: string
  connectionId: string
  draftScope?: string
  persist: () => T
  promote: (draftScope: string, connectionId: string, baseUrl: string) => Promise<void>
}

export async function saveWithRegistryAuthPromotion<T>(options: RegistryAuthPromotionSaveOptions<T>): Promise<T> {
  if (options.draftScope) {
    await options.promote(
      validateRegistryAuthScope(options.draftScope),
      validateRegistryAuthScope(options.connectionId),
      options.baseUrl
    )
  }

  return options.persist()
}

interface VerifyTokenRegistryAuthOptions {
  baseUrl: string
  headers: Record<string, string>
  token: string
  readStatus: (
    url: string,
    token: string,
    headers: Record<string, string>
  ) => Promise<{ version?: null | string }>
  resolveWebSocketUrl: (baseUrl: string, token: string) => Promise<null | string> | null | string
  probeWebSocket: (
    url: string,
    headers: Record<string, string>
  ) => Promise<{ ok: boolean; reason?: string }>
}

export async function verifyTokenRegistryAuth(options: VerifyTokenRegistryAuthOptions): Promise<{ version: null | string }> {
  const token = options.token.trim()

  if (!token) {
    throw new Error('Remote gateway session token is required.')
  }

  const status = await options.readStatus(`${options.baseUrl}/api/status`, token, options.headers)
  const wsUrl = await options.resolveWebSocketUrl(options.baseUrl, token)

  if (!wsUrl) {
    throw new Error('The gateway WebSocket readiness URL could not be resolved.')
  }

  const probe = await options.probeWebSocket(wsUrl, options.headers)

  if (!probe.ok) {
    throw new Error(`The authenticated gateway WebSocket readiness check failed: ${probe.reason || 'connection failed'}`)
  }

  return { version: status.version || null }
}

export function serializeRegistryAuthFailure(error: unknown, displayText: string): RegistryAuthFailure {
  return {
    error: displayText,
    kind: isGatewayAuthRejection(error) ? 'auth-required' : 'transport-error',
    ok: false
  }
}
