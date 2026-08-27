import { profileScopeKey } from '@/api/client'

import type { SessionOwnerRoute } from './session-request-router'

const CANONICAL_PREFIX = 'hermes-composer-scope:v1:'
const LEGACY_SEPARATOR = '\0'
const LEGACY_NEW_CHAT = '__new__'

export type ComposerStorageOwner = Pick<SessionOwnerRoute, 'connectionId' | 'profile'>

export interface NormalizedComposerStorageOwner {
  connectionId: string
  profile: string
}

export interface ComposerStorageScope {
  format: 'canonical' | 'legacy'
  owner: NormalizedComposerStorageOwner
  /** Raw durable/lineage-root id. Null is the New Chat identity. */
  storedSessionId: string | null
}

interface DecodeComposerStorageScopeOptions {
  /**
   * Compatibility is deliberately owner-gated: profileScopeKey's local form
   * omits the connection id, so a legacy key cannot prove its own exact owner.
   * Callers may provide the owner only while migrating that owner's old key;
   * RPC/drain paths omit this option and therefore accept canonical keys only.
   */
  legacyOwner?: ComposerStorageOwner
}

export function normalizeComposerStorageOwner(owner: ComposerStorageOwner): NormalizedComposerStorageOwner {
  return {
    connectionId: owner.connectionId.trim() || 'local',
    profile: owner.profile.trim() || 'default'
  }
}

function validStoredSessionId(storedSessionId: unknown): storedSessionId is string | null {
  return storedSessionId === null || (typeof storedSessionId === 'string' && storedSessionId.trim().length > 0)
}

/**
 * Reversible renderer-storage identity. The versioned JSON payload avoids
 * delimiter parsing and keeps exact connection/profile ownership alongside the
 * raw durable id (or null for New Chat). This key is renderer-only.
 */
export function encodeComposerStorageScopeKey(
  owner: ComposerStorageOwner,
  storedSessionId: string | null
): string {
  if (!validStoredSessionId(storedSessionId)) {
    throw new Error('Composer storage scope requires a stored session id or null')
  }

  const normalizedOwner = normalizeComposerStorageOwner(owner)

  return `${CANONICAL_PREFIX}${JSON.stringify([
    normalizedOwner.connectionId,
    normalizedOwner.profile,
    storedSessionId
  ])}`
}

/**
 * The pre-codec profileScopeKey + NUL shape. Kept in one place solely so a
 * migration worker can find and re-home old entries; never pass its output to a
 * drain/RPC path.
 */
export function legacyComposerStorageScopeKey(
  owner: ComposerStorageOwner,
  storedSessionId: string | null
): string {
  if (!validStoredSessionId(storedSessionId)) {
    throw new Error('Composer storage scope requires a stored session id or null')
  }

  const normalizedOwner = normalizeComposerStorageOwner(owner)
  const ownerKey = profileScopeKey(normalizedOwner)

  return `${ownerKey}${LEGACY_SEPARATOR}${storedSessionId ?? LEGACY_NEW_CHAT}`
}

function decodeCanonical(key: string): ComposerStorageScope | null {
  if (!key.startsWith(CANONICAL_PREFIX)) {
    return null
  }

  try {
    const payload: unknown = JSON.parse(key.slice(CANONICAL_PREFIX.length))

    if (
      !Array.isArray(payload) ||
      payload.length !== 3 ||
      typeof payload[0] !== 'string' ||
      typeof payload[1] !== 'string' ||
      !validStoredSessionId(payload[2])
    ) {
      return null
    }

    const owner = normalizeComposerStorageOwner({ connectionId: payload[0], profile: payload[1] })
    const storedSessionId = payload[2]

    // Reject alternate spellings instead of silently normalizing an untrusted
    // key into a backend target. Only encodeComposerStorageScopeKey output is a
    // canonical drainable identity.
    if (encodeComposerStorageScopeKey(owner, storedSessionId) !== key) {
      return null
    }

    return { format: 'canonical', owner, storedSessionId }
  } catch {
    return null
  }
}

function decodeLegacy(key: string, ownerInput: ComposerStorageOwner): ComposerStorageScope | null {
  const owner = normalizeComposerStorageOwner(ownerInput)
  const prefix = `${profileScopeKey(owner)}${LEGACY_SEPARATOR}`

  if (!key.startsWith(prefix)) {
    return null
  }

  const identity = key.slice(prefix.length)

  if (!identity || identity.includes(LEGACY_SEPARATOR)) {
    return null
  }

  return {
    format: 'legacy',
    owner,
    storedSessionId: identity === LEGACY_NEW_CHAT ? null : identity
  }
}

/**
 * Decode canonical keys fail-closed. Legacy decoding is opt-in and exact-owner
 * gated for migration only; omitting legacyOwner is the safe drain/RPC mode.
 */
export function decodeComposerStorageScopeKey(
  key: string,
  options: DecodeComposerStorageScopeOptions = {}
): ComposerStorageScope | null {
  const canonical = decodeCanonical(key)

  if (canonical || !options.legacyOwner) {
    return canonical
  }

  return decodeLegacy(key, options.legacyOwner)
}
