import { createHash, randomBytes } from 'node:crypto'

import { isGatewayAuthRejection, normalizeRemoteBaseUrl } from './connection-config'
import { connectionDialFieldsChanged, type RegistryConnection } from './connection-registry'

const DURABLE_SCOPE_RE = /^(?!draft-)[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/
const DRAFT_SCOPE_RE = /^draft-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i

export type RegistryAuthFailureKind = 'auth-required' | 'transport-error'

export interface RegistryAuthCandidateBinding {
  authMode: 'oauth' | 'token'
  baseUrl: string
  connectionId: string
  generation: number
  headersFingerprint: string
  scope: string
  tokenFingerprint: string
}

interface RegistryAuthCandidateInput {
  authMode: unknown
  baseUrl: unknown
  connectionId: unknown
  generation: unknown
  headers?: Record<string, string>
  scope: unknown
  token?: string
}

function sha256(value: string): string {
  return createHash('sha256').update(value).digest('hex')
}

export function fingerprintRegistryAuthCandidate(
  headers: Record<string, string> = {},
  token = ''
): Pick<RegistryAuthCandidateBinding, 'headersFingerprint' | 'tokenFingerprint'> {
  const canonicalHeaders = Object.entries(headers)
    .map(([name, value]) => [name.toLowerCase(), String(value)] as const)
    .sort(([leftName, leftValue], [rightName, rightValue]) =>
      leftName.localeCompare(rightName) || leftValue.localeCompare(rightValue)
    )

  return {
    headersFingerprint: sha256(JSON.stringify(canonicalHeaders)),
    tokenFingerprint: sha256(token)
  }
}

export function registryAuthCandidateBinding(input: RegistryAuthCandidateInput): RegistryAuthCandidateBinding {
  const authMode = String(input.authMode || '').trim().toLowerCase()

  if (authMode !== 'oauth' && authMode !== 'token') {
    throw new Error('A valid registered gateway authentication mode is required.')
  }

  const generation = Number(input.generation)

  if (!Number.isSafeInteger(generation) || generation < 0) {
    throw new Error('A valid registered gateway authentication generation is required.')
  }

  return {
    authMode,
    baseUrl: normalizeRemoteBaseUrl(input.baseUrl),
    connectionId: validateRegistryAuthScope(input.connectionId),
    generation,
    ...fingerprintRegistryAuthCandidate(input.headers, input.token || ''),
    scope: validateRegistryAuthScope(input.scope)
  }
}

interface RegistryAuthReadinessAuthorityOptions {
  now?: () => number
  randomToken?: () => string
  ttlMs?: number
}

export class RegistryAuthReadinessAuthority {
  private readonly capabilities = new Map<string, RegistryAuthCandidateBinding & { expiresAt: number }>()
  private readonly drafts = new Map<string, string>()
  private readonly generations = new Map<string, number>()
  private readonly now: () => number
  private readonly randomToken: () => string
  private readonly ttlMs: number

  constructor(options: RegistryAuthReadinessAuthorityOptions = {}) {
    this.now = options.now || Date.now
    this.randomToken = options.randomToken || (() => randomBytes(32).toString('base64url'))
    this.ttlMs = options.ttlMs ?? 60_000
  }

  issue(binding: RegistryAuthCandidateBinding): string {
    const capability = this.randomToken()
    this.capabilities.set(capability, { ...binding, expiresAt: this.now() + this.ttlMs })

    return capability
  }

  registerDraft(rawScope: unknown, targetConnectionId: string): void {
    const scope = validateRegistryAuthScope(rawScope)

    if (!DRAFT_SCOPE_RE.test(scope)) {
      throw new Error('A valid draft gateway authentication scope is required.')
    }

    this.drafts.set(scope, validateRegistryAuthScope(targetConnectionId))
    this.generations.set(scope, 0)
  }

  ownsDraft(rawScope: unknown, ownerConnectionId?: string): boolean {
    const scope = validateRegistryAuthScope(rawScope)

    if (!this.drafts.has(scope)) {
      return false
    }

    const owner = this.drafts.get(scope)

    return ownerConnectionId === undefined || owner === ownerConnectionId
  }

  draftOwner(rawScope: unknown): string {
    const scope = validateRegistryAuthScope(rawScope)

    if (!this.drafts.has(scope)) {
      throw new Error('The draft gateway authentication scope is not registered.')
    }

    return this.drafts.get(scope)!
  }

  generationForScope(rawScope: unknown): number {
    const scope = validateRegistryAuthScope(rawScope)

    return this.generations.get(scope) || 0
  }

  invalidateScope(rawScope: unknown): void {
    const scope = validateRegistryAuthScope(rawScope)

    for (const [capability, record] of this.capabilities) {
      if (record.scope === scope) {
        this.capabilities.delete(capability)
      }
    }

    this.drafts.delete(scope)
    this.generations.set(scope, this.generationForScope(scope) + 1)
  }

  consume(capability: string | undefined, binding: RegistryAuthCandidateBinding): void {
    const record = capability ? this.capabilities.get(capability) : undefined

    if (capability) {
      this.capabilities.delete(capability)
    }

    if (!record) {
      throw new Error('A valid readiness capability is required; it is unknown or already used.')
    }

    if (record.expiresAt <= this.now()) {
      throw new Error('The readiness capability has expired.')
    }

    const { expiresAt: _expiresAt, ...recordBinding } = record

    if (JSON.stringify(recordBinding) !== JSON.stringify(binding)) {
      throw new Error('The readiness capability does not match the connection candidate.')
    }
  }
}

export interface RegistryAuthScopeAuthority {
  baseUrl: string
  connectionId: string
  generation: number
  scope: string
}

interface RegistryAuthRegistryRow {
  id: string
  kind: string
  url?: string
}

export function resolveRegistryAuthScopeAuthority(options: {
  authority: RegistryAuthReadinessAuthority
  baseUrl: unknown
  registry: { connections: RegistryAuthRegistryRow[] }
  scope: unknown
}): RegistryAuthScopeAuthority {
  const scope = validateRegistryAuthScope(options.scope)
  const callerBaseUrl = normalizeRemoteBaseUrl(options.baseUrl)

  if (DRAFT_SCOPE_RE.test(scope)) {
    const ownerConnectionId = options.authority.draftOwner(scope)

    return {
      baseUrl: callerBaseUrl,
      connectionId: ownerConnectionId || scope,
      generation: options.authority.generationForScope(scope),
      scope
    }
  }

  const connection = options.registry.connections.find(row => row.id === scope)

  if (!connection || connection.kind !== 'remote' || !connection.url) {
    throw new Error('The registered gateway authentication scope does not identify a remote connection.')
  }

  const storedBaseUrl = normalizeRemoteBaseUrl(connection.url)

  if (storedBaseUrl !== callerBaseUrl) {
    throw new Error('The caller URL does not match the registered gateway authentication scope.')
  }

  return {
    baseUrl: storedBaseUrl,
    connectionId: connection.id,
    generation: options.authority.generationForScope(scope),
    scope
  }
}

export async function saveVerifiedRegistryConnection<T>(options: {
  authority: RegistryAuthReadinessAuthority
  binding: RegistryAuthCandidateBinding
  capability?: string
  connectionId: string
  persist: () => Promise<T> | T
  promote?: () => Promise<void>
  readinessRequired?: boolean
}): Promise<T> {
  if (options.readinessRequired !== false || options.promote) {
    options.authority.consume(options.capability, options.binding)

    if (validateRegistryAuthScope(options.connectionId) !== options.binding.connectionId) {
      throw new Error('The readiness capability target does not match the saved connection.')
    }
  }

  if (options.promote) {
    await options.promote()
  }

  return options.persist()
}

export function registryAuthReadinessRequired(
  existing: RegistryConnection | null | undefined,
  candidate: RegistryConnection
): boolean {
  return !existing || connectionDialFieldsChanged(existing, candidate)
}

export interface RegistryAuthFailure {
  error: string
  kind: RegistryAuthFailureKind
  ok: false
  statusCode: number | null
}

export function resolveRegistryAuthCandidateHeaders<T>(
  rawCandidate: unknown,
  storedHeaders: Record<string, T>
): Record<string, string | T> {
  if (!rawCandidate || typeof rawCandidate !== 'object' || Array.isArray(rawCandidate)) {
    return { ...storedHeaders }
  }

  const candidate: Record<string, string | T> = {}

  for (const [rawName, value] of Object.entries(rawCandidate)) {
    const name = rawName.trim()

    if (!name) {
      continue
    }

    if (value === null) {
      if (storedHeaders[name] !== undefined) {
        candidate[name] = storedHeaders[name]
      }

      continue
    }

    if (typeof value === 'string' && value.trim()) {
      candidate[name] = value.trim()
    } else if (value && typeof value === 'object') {
      candidate[name] = value as T
    }
  }

  return candidate
}

export interface AuthenticatedRegistryStatusInput {
  authMode: 'oauth' | 'token'
  baseUrl: string
  headers: Record<string, string>
  scope: string
  token?: string | null
}

export interface AuthenticatedRegistryStatusDependencies<T> {
  readBearerStatus: (url: string, bearer: string, headers: Record<string, string>) => Promise<T>
  readNativeAccessToken: (baseUrl: string, scope: string) => Promise<null | string>
  readOauthStatus: (url: string, scope: string, headers: Record<string, string>) => Promise<T>
  readTokenStatus: (url: string, token: string, headers: Record<string, string>) => Promise<T>
}

export async function authenticatedRegistryStatus<T>(
  input: AuthenticatedRegistryStatusInput,
  dependencies: AuthenticatedRegistryStatusDependencies<T>
): Promise<T> {
  const baseUrl = normalizeRemoteBaseUrl(input.baseUrl)
  const url = `${baseUrl}/api/status`

  if (input.authMode === 'oauth') {
    const bearer = await dependencies.readNativeAccessToken(baseUrl, validateRegistryAuthScope(input.scope))

    return bearer
      ? dependencies.readBearerStatus(url, bearer, input.headers)
      : dependencies.readOauthStatus(url, input.scope, input.headers)
  }

  const token = String(input.token || '').trim()

  if (!token) {
    throw new Error('Remote gateway session token is required.')
  }

  return dependencies.readTokenStatus(url, token, input.headers)
}

export async function readDurableRegistryAuthStatus<T>(
  input: AuthenticatedRegistryStatusInput,
  dependencies: AuthenticatedRegistryStatusDependencies<T>
): Promise<{ baseUrl: string; connected: true; ok: true } | RegistryAuthFailure> {
  const baseUrl = normalizeRemoteBaseUrl(input.baseUrl)

  try {
    await authenticatedRegistryStatus({ ...input, baseUrl }, dependencies)

    return { baseUrl, connected: true, ok: true }
  } catch (error) {
    return serializeRegistryAuthFailure(error, 'Could not read gateway auth status.')
  }
}

interface AuthenticatedRegistryConnectionTestDependencies<T extends { version?: null | string }>
  extends AuthenticatedRegistryStatusDependencies<T> {
  onStatus?: (status: T) => void
  probeWebSocket: (url: string, headers: Record<string, string>) => Promise<{ ok: boolean; reason?: string }>
  resolveWebSocketUrl: (
    baseUrl: string,
    authMode: 'oauth' | 'token',
    token: null | string,
    headers: Record<string, string>,
    scope: string
  ) => Promise<null | string> | null | string
}

export async function testAuthenticatedRegistryConnection<T extends { version?: null | string }>(
  input: AuthenticatedRegistryStatusInput,
  dependencies: AuthenticatedRegistryConnectionTestDependencies<T>
): Promise<{ baseUrl: string; ok: true; version: null | string } | RegistryAuthFailure> {
  const baseUrl = normalizeRemoteBaseUrl(input.baseUrl)

  return runStructuredRegistryTest(async () => {
    const status = await authenticatedRegistryStatus({ ...input, baseUrl }, dependencies)

    dependencies.onStatus?.(status)

    const wsUrl = await dependencies.resolveWebSocketUrl(
      baseUrl,
      input.authMode,
      input.token || null,
      input.headers,
      input.scope
    )

    if (!wsUrl) {
      throw new Error('The gateway WebSocket readiness URL could not be resolved.')
    }

    const probe = await dependencies.probeWebSocket(wsUrl, input.headers)

    if (!probe.ok) {
      throw new Error(`The authenticated gateway WebSocket readiness check failed: ${probe.reason || 'connection failed'}`)
    }

    return { baseUrl, ok: true as const, version: status.version || null }
  }, 'Could not test the registered gateway connection.')
}

export async function runStructuredRegistryTest<T extends object>(
  operation: () => Promise<T>,
  failureText: string
): Promise<T | RegistryAuthFailure> {
  try {
    return await operation()
  } catch (error) {
    return serializeRegistryAuthFailure(error, failureText)
  }
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

export function createRegistryAuthTargetConnectionId(randomUuid: () => string = () => crypto.randomUUID()): string {
  return validateRegistryAuthScope(`gateway-${randomUuid()}`)
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

export interface RegistryAuthCredentialSnapshot {
  cookies: readonly unknown[]
  nativeTokens?: unknown
}

export interface RegistryAuthCredentialLifecycle {
  snapshot: (scope: string, baseUrl: string) => Promise<RegistryAuthCredentialSnapshot>
  replace: (fromScope: string, toScope: string, baseUrl: string) => Promise<void>
  restore: (scope: string, baseUrl: string, snapshot: RegistryAuthCredentialSnapshot) => Promise<void>
  clear: (scope: string, baseUrl: string) => Promise<void>
}

export class RegistryAuthCleanupRetryQueue {
  private readonly pending = new Map<string, { baseUrl: string; scope: string }>()

  get size(): number {
    return this.pending.size
  }

  record(scope: string, baseUrl: string): void {
    const validatedScope = validateRegistryAuthScope(scope)
    this.pending.set(`${validatedScope}\n${baseUrl}`, { baseUrl, scope: validatedScope })
  }

  async retry(lifecycle: RegistryAuthCredentialLifecycle, onFailure?: (error: unknown) => void): Promise<void> {
    for (const [key, entry] of this.pending) {
      try {
        await lifecycle.clear(entry.scope, entry.baseUrl)
        this.pending.delete(key)
      } catch (error) {
        onFailure?.(error)
        // Keep the Electron-memory entry for the next lifecycle call.
      }
    }
  }
}

const defaultRegistryAuthCleanupRetries = new RegistryAuthCleanupRetryQueue()

export class RegistryAuthRollbackError extends AggregateError {
  readonly stateUncertain = true

  constructor(primaryError: unknown, rollbackError: unknown, message: string) {
    super([primaryError, rollbackError], message)
    this.name = 'RegistryAuthRollbackError'
  }
}

async function restoreRegistryAuthAfterFailure(
  primaryError: unknown,
  restore: () => Promise<void>,
  aggregateMessage: string
): Promise<never> {
  try {
    await restore()
  } catch (rollbackError) {
    throw new RegistryAuthRollbackError(primaryError, rollbackError, aggregateMessage)
  }

  throw primaryError
}

export async function clearRegistryAuthCredentialsTransactionally(options: {
  clear: () => Promise<void>
  restore: (snapshot: RegistryAuthCredentialSnapshot) => Promise<void>
  snapshot: () => Promise<RegistryAuthCredentialSnapshot>
}): Promise<void> {
  const before = await options.snapshot()

  try {
    await options.clear()
  } catch (error) {
    return restoreRegistryAuthAfterFailure(
      error,
      () => options.restore(before),
      'Registry authentication credential cleanup failed and its rollback also failed; credential state is uncertain.'
    )
  }
}

export async function removeRegistryConnectionTransactionally<T>(options: {
  credentials?: {
    clear: () => Promise<void>
    restore: (snapshot: RegistryAuthCredentialSnapshot) => Promise<void>
    snapshot: () => Promise<RegistryAuthCredentialSnapshot>
  }
  persistRemoval: () => Promise<T> | T
}): Promise<T> {
  const credentialsBefore = options.credentials ? await options.credentials.snapshot() : undefined

  if (options.credentials) {
    await options.credentials.clear()
  }

  try {
    return await options.persistRemoval()
  } catch (error) {
    if (!options.credentials || !credentialsBefore) {
      throw error
    }

    return restoreRegistryAuthAfterFailure(
      error,
      () => options.credentials!.restore(credentialsBefore),
      'Registry connection removal failed and its credential rollback also failed; credential state is uncertain.'
    )
  }
}

export async function promoteAndPersistRegistryAuth<T>(options: {
  baseUrl: string
  cleanupRetries?: RegistryAuthCleanupRetryQueue
  fromScope: unknown
  lifecycle: RegistryAuthCredentialLifecycle
  onCleanupFailure?: (error: unknown) => void
  persist: () => Promise<T> | T
  toScope: unknown
}): Promise<T> {
  const fromScope = validateRegistryAuthScope(options.fromScope)
  const toScope = validateRegistryAuthScope(options.toScope)
  const destinationBefore = await options.lifecycle.snapshot(toScope, options.baseUrl)
  let result: T

  try {
    await options.lifecycle.replace(fromScope, toScope, options.baseUrl)
    result = await options.persist()
  } catch (error) {
    return restoreRegistryAuthAfterFailure(
      error,
      () => options.lifecycle.restore(toScope, options.baseUrl, destinationBefore),
      'Registry authentication promotion failed and its credential rollback also failed; credential state is uncertain.'
    )
  }

  try {
    await options.lifecycle.clear(fromScope, options.baseUrl)
  } catch (error) {
    ;(options.cleanupRetries || defaultRegistryAuthCleanupRetries).record(fromScope, options.baseUrl)
    options.onCleanupFailure?.(error)
  }

  return result
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

export async function revokeRegistryAuthScope(
  rawScope: unknown,
  baseUrl: string,
  authority: RegistryAuthReadinessAuthority,
  deps: RegistryAuthCleanupDependencies
): Promise<void> {
  const scope = validateRegistryAuthScope(rawScope)

  try {
    await clearRegistryAuthScope(scope, baseUrl, deps)
  } finally {
    authority.invalidateScope(scope)
  }
}

export async function teardownRemovedRegistryConnection(options: {
  authority: RegistryAuthReadinessAuthority
  clearCredentials?: () => Promise<void>
  scope: unknown
  stopBackends: () => Promise<void>
}): Promise<void> {
  const scope = validateRegistryAuthScope(options.scope)
  let cleanupFailure: { error: unknown } | undefined
  let stopFailure: { error: unknown } | undefined

  try {
    await options.stopBackends()
  } catch (error) {
    stopFailure = { error }
  } finally {
    try {
      await options.clearCredentials?.()
    } catch (error) {
      cleanupFailure = { error }
    } finally {
      options.authority.invalidateScope(scope)
    }
  }

  if (cleanupFailure) {
    if (stopFailure) {
      throw new AggregateError(
        [stopFailure.error, cleanupFailure.error],
        'Connection backend teardown and registry authentication cleanup both failed.'
      )
    }

    throw cleanupFailure.error
  }

  if (stopFailure) {
    throw stopFailure.error
  }
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
  const rawStatusCode = error && typeof error === 'object' ? Number((error as { statusCode?: unknown }).statusCode) : NaN

  return {
    error: displayText,
    kind: isGatewayAuthRejection(error) ? 'auth-required' : 'transport-error',
    ok: false,
    statusCode: Number.isInteger(rawStatusCode) ? rawStatusCode : null
  }
}
