import { profileScopeKey } from '@/api/client'

import type { SessionOwnerRoute } from './session-request-router'

const CANONICAL_PREFIX = 'hermes-composer-scope:v2:'
const LEGACY_SEPARATOR = '\0'
const LEGACY_NEW_CHAT = '__new__'
const MAX_ALIAS_DEPTH = 64
const STORAGE_SCOPE_ALIASES_KEY = 'hermes.desktop.composerStorageScopeAliases.v1'
const composerStorageScopeAliases = new Map<string, string>()

export type ComposerStorageOwner = Pick<SessionOwnerRoute, 'connectionId' | 'profile'>

export interface NormalizedComposerStorageOwner {
  connectionId: string
  profile: string
}

export type ComposerNewChatGeneration = number | string

export interface ComposerStorageScope {
  format: 'canonical' | 'legacy'
  owner: NormalizedComposerStorageOwner
  /** Raw durable/lineage-root id. Null is the New Chat identity. */
  storedSessionId: string | null
  /** Distinguishes successive New Chat drafts for the same owner. */
  newChatGeneration: ComposerNewChatGeneration
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

const UUID_GENERATION_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i

function validNewChatGeneration(value: unknown): value is ComposerNewChatGeneration {
  return (
    (Number.isSafeInteger(value) && (value as number) >= 0) ||
    (typeof value === 'string' && UUID_GENERATION_RE.test(value))
  )
}

/**
 * Reversible renderer-storage identity. The versioned JSON payload avoids
 * delimiter parsing and keeps exact connection/profile ownership alongside the
 * raw durable id (or null for New Chat). This key is renderer-only.
 */
export function encodeComposerStorageScopeKey(
  owner: ComposerStorageOwner,
  storedSessionId: string | null,
  newChatGeneration: ComposerNewChatGeneration = 0
): string {
  if (!validStoredSessionId(storedSessionId)) {
    throw new Error('Composer storage scope requires a stored session id or null')
  }

  const normalizedOwner = normalizeComposerStorageOwner(owner)
  const normalizedGeneration = storedSessionId === null ? newChatGeneration : 0

  if (!validNewChatGeneration(normalizedGeneration)) {
    throw new Error('Composer New Chat generation must be a non-negative legacy integer or UUID')
  }

  return `${CANONICAL_PREFIX}${JSON.stringify([
    normalizedOwner.connectionId,
    normalizedOwner.profile,
    storedSessionId,
    normalizedGeneration
  ])}`
}

/**
 * The pre-codec profileScopeKey + NUL shape. Kept in one place solely so a
 * migration worker can find and re-home old entries; never pass its output to a
 * drain/RPC path.
 */
export function legacyComposerStorageScopeKey(owner: ComposerStorageOwner, storedSessionId: string | null): string {
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
      payload.length !== 4 ||
      typeof payload[0] !== 'string' ||
      typeof payload[1] !== 'string' ||
      !validStoredSessionId(payload[2]) ||
      !validNewChatGeneration(payload[3])
    ) {
      return null
    }

    const owner = normalizeComposerStorageOwner({ connectionId: payload[0], profile: payload[1] })
    const storedSessionId = payload[2]
    const newChatGeneration = storedSessionId === null ? payload[3] : 0

    // Reject alternate spellings instead of silently normalizing an untrusted
    // key into a backend target. Only encodeComposerStorageScopeKey output is a
    // canonical drainable identity.
    if (encodeComposerStorageScopeKey(owner, storedSessionId, newChatGeneration) !== key) {
      return null
    }

    return { format: 'canonical', newChatGeneration, owner, storedSessionId }
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
    newChatGeneration: 0,
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

/** Follow an explicit same-owner storage handoff to its latest key. */
export function resolveComposerStorageScopeKey(scopeKey: string): string {
  let current = scopeKey
  const seen = new Set<string>()

  for (let depth = 0; depth < MAX_ALIAS_DEPTH; depth += 1) {
    if (seen.has(current)) {
      return scopeKey
    }

    seen.add(current)
    const next = composerStorageScopeAliases.get(current)

    if (!next) {
      return current
    }

    current = next
  }

  return scopeKey
}

function validAlias(fromScopeKey: string, toScopeKey: string): boolean {
  const from = decodeComposerStorageScopeKey(fromScopeKey)
  const to = decodeComposerStorageScopeKey(toScopeKey)

  return Boolean(
    from &&
    to &&
    fromScopeKey !== toScopeKey &&
    from.owner.connectionId === to.owner.connectionId &&
    from.owner.profile === to.owner.profile
  )
}

function readPersistedComposerStorageScopeAliases(): Map<string, string> {
  const aliases = new Map<string, string>()

  if (typeof window === 'undefined') {
    return aliases
  }

  try {
    const raw = window.localStorage.getItem(STORAGE_SCOPE_ALIASES_KEY)
    const parsed: unknown = raw ? JSON.parse(raw) : null

    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return aliases
    }

    for (const [from, to] of Object.entries(parsed)) {
      if (typeof to === 'string' && validAlias(from, to)) {
        aliases.set(from, to)
      }
    }
  } catch {
    return aliases
  }

  return aliases
}

function persistComposerStorageScopeAliases(aliases: Map<string, string>): void {
  if (typeof window === 'undefined') {
    return
  }

  try {
    if (aliases.size === 0) {
      window.localStorage.removeItem(STORAGE_SCOPE_ALIASES_KEY)
    } else {
      window.localStorage.setItem(STORAGE_SCOPE_ALIASES_KEY, JSON.stringify(Object.fromEntries(aliases)))
    }
  } catch {
    // Best effort: aliases remain valid inside this renderer.
  }
}

function reloadComposerStorageScopeAliases(): void {
  const persisted = readPersistedComposerStorageScopeAliases()

  composerStorageScopeAliases.clear()

  for (const [from, to] of persisted) {
    composerStorageScopeAliases.set(from, to)
  }
}

/** Publish before migrated state so peer renderers retire the source first. */
export function publishComposerStorageScopeAlias(fromScopeKey: string, toScopeKey: string): boolean {
  const target = resolveComposerStorageScopeKey(toScopeKey)

  if (!validAlias(fromScopeKey, target)) {
    return false
  }

  const persisted = readPersistedComposerStorageScopeAliases()

  persisted.set(fromScopeKey, target)
  persistComposerStorageScopeAliases(persisted)

  return true
}

/** Activate an already-published alias in this renderer after local state moves. */
export function activateComposerStorageScopeAlias(fromScopeKey: string, toScopeKey: string): void {
  const target = resolveComposerStorageScopeKey(toScopeKey)

  if (validAlias(fromScopeKey, target)) {
    composerStorageScopeAliases.set(fromScopeKey, target)
  }
}

/** @internal — callers must validate exact same-owner canonical scopes first. */
export function registerComposerStorageScopeAlias(fromScopeKey: string, toScopeKey: string): void {
  if (publishComposerStorageScopeAlias(fromScopeKey, toScopeKey)) {
    activateComposerStorageScopeAlias(fromScopeKey, toScopeKey)
  }
}

/** @internal */
export function _resetComposerStorageScopeAliasesForTests(): void {
  composerStorageScopeAliases.clear()

  if (typeof window !== 'undefined') {
    window.localStorage.removeItem(STORAGE_SCOPE_ALIASES_KEY)
  }
}

/** @internal */
export function _reloadComposerStorageScopeAliasesForTests(): void {
  reloadComposerStorageScopeAliases()
}

reloadComposerStorageScopeAliases()

if (typeof window !== 'undefined') {
  window.addEventListener('storage', event => {
    if (event.key === STORAGE_SCOPE_ALIASES_KEY) {
      reloadComposerStorageScopeAliases()
    }
  })
}
