import { registryBackendScopeKey } from '@hermes/shared'
import { atom } from 'nanostores'

export interface HostCapabilityVersion {
  major: number
  minor: number
}

export interface HostCapabilityDescriptor {
  endpoints: string[]
  version: HostCapabilityVersion
}

export type HostCapabilities = Record<string, HostCapabilityDescriptor>

const CAPABILITY_NAME = /^[a-z][a-z0-9-]{0,63}$/
const ENDPOINT_NAME = /^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+$/
const MAX_CAPABILITIES = 64
const MAX_ENDPOINTS = 32
const MAX_ENDPOINT_LENGTH = 128
const MAX_VERSION = 1_000

const cachedByScope = new Map<string, HostCapabilities>()
let activeScope = 'default'

export const $hostCapabilities = atom<HostCapabilities>({})

const plainObject = (value: unknown): value is Record<string, unknown> =>
  Boolean(value) && typeof value === 'object' && !Array.isArray(value)

function normalizeDescriptor(value: unknown): HostCapabilityDescriptor | null {
  if (!plainObject(value) || Object.keys(value).some(key => key !== 'endpoints' && key !== 'version')) {
    return null
  }

  const version = value.version
  const endpoints = value.endpoints

  if (
    !plainObject(version) ||
    Object.keys(version).some(key => key !== 'major' && key !== 'minor') ||
    !Number.isSafeInteger(version.major) ||
    !Number.isSafeInteger(version.minor) ||
    Number(version.major) < 1 ||
    Number(version.major) > MAX_VERSION ||
    Number(version.minor) < 0 ||
    Number(version.minor) > MAX_VERSION ||
    !Array.isArray(endpoints) ||
    endpoints.length === 0 ||
    endpoints.length > MAX_ENDPOINTS
  ) {
    return null
  }

  const normalizedEndpoints: string[] = []
  const unique = new Set<string>()

  for (const endpoint of endpoints) {
    if (
      typeof endpoint !== 'string' ||
      endpoint.length > MAX_ENDPOINT_LENGTH ||
      !ENDPOINT_NAME.test(endpoint) ||
      unique.has(endpoint)
    ) {
      return null
    }

    unique.add(endpoint)
    normalizedEndpoints.push(endpoint)
  }

  return {
    endpoints: normalizedEndpoints,
    version: { major: Number(version.major), minor: Number(version.minor) }
  }
}

export function normalizeHostCapabilities(value: unknown): HostCapabilities {
  if (!plainObject(value)) {
    return {}
  }

  const entries = Object.entries(value)

  if (entries.length > MAX_CAPABILITIES) {
    return {}
  }

  const normalized: HostCapabilities = {}

  for (const [name, descriptor] of entries) {
    if (!CAPABILITY_NAME.test(name)) {
      continue
    }

    const accepted = normalizeDescriptor(descriptor)

    if (accepted) {
      normalized[name] = accepted
    }
  }

  return normalized
}

export function hostCapabilityScope(
  connectionId: null | string | undefined,
  profile: null | string | undefined
): string {
  const normalizedProfile = profile?.trim() || 'default'

  return connectionId ? registryBackendScopeKey(connectionId, normalizedProfile) : normalizedProfile
}

export function ingestHostCapabilities(value: unknown, scope = activeScope): void {
  const normalized = normalizeHostCapabilities(value)

  cachedByScope.set(scope, normalized)

  if (scope === activeScope) {
    $hostCapabilities.set(normalized)
  }
}

export function activateHostCapabilities(scope: string): void {
  activeScope = scope
  $hostCapabilities.set(cachedByScope.get(scope) ?? {})
}

export function clearHostCapabilities(scope: string): void {
  cachedByScope.delete(scope)

  if (scope === activeScope) {
    $hostCapabilities.set({})
  }
}

export function resetHostCapabilities(): void {
  cachedByScope.clear()
  $hostCapabilities.set({})
}
