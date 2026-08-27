import { registryBackendScopeKey } from '@hermes/shared'

import type { ClientSessionState } from '../types'

export const DEFAULT_WARM_SESSION_TRANSCRIPT_COUNT = 24
export const DEFAULT_WARM_SESSION_TRANSCRIPT_BYTES = 32 * 1024 * 1024

export interface SessionStateOwner {
  connectionId?: null | string
  profile: string
  storedSessionId: string
}

export type RuntimeSessionStateOwner = Pick<SessionStateOwner, 'connectionId' | 'profile'>

const OWNED_RUNTIME_PREFIX = 'owner:'

function ownedRuntimeKey(runtimeId: string, owner: RuntimeSessionStateOwner): string {
  const connectionId = owner.connectionId?.trim()

  return connectionId
    ? `${OWNED_RUNTIME_PREFIX}${JSON.stringify([connectionId, owner.profile.trim() || 'default', runtimeId])}`
    : runtimeId
}

function publicRuntimeId(key: string): string {
  if (!key.startsWith(OWNED_RUNTIME_PREFIX)) {
    return key
  }

  try {
    const parsed = JSON.parse(key.slice(OWNED_RUNTIME_PREFIX.length)) as unknown[]

    return typeof parsed[2] === 'string' ? parsed[2] : key
  } catch {
    return key
  }
}

const OWNED_STORED_PREFIX = 'stored-owner:'

function ownedStoredKey(storedSessionId: string, owner: RuntimeSessionStateOwner): string {
  const connectionId = owner.connectionId?.trim()

  return connectionId
    ? `${OWNED_STORED_PREFIX}${JSON.stringify([connectionId, owner.profile.trim() || 'default', storedSessionId])}`
    : storedSessionId
}

function publicStoredId(key: string): string {
  if (!key.startsWith(OWNED_STORED_PREFIX)) {
    return key
  }

  try {
    const parsed = JSON.parse(key.slice(OWNED_STORED_PREFIX.length)) as unknown[]

    return typeof parsed[2] === 'string' ? parsed[2] : key
  } catch {
    return key
  }
}

export class SessionRuntimeIndex extends Map<string, string> {
  override get(storedSessionId: string): string | undefined {
    if (super.has(storedSessionId)) {
      return super.get(storedSessionId)
    }

    const matches = [...super.entries()].filter(([key]) => publicStoredId(key) === storedSessionId)

    return matches.length === 1 ? matches[0][1] : undefined
  }

  getOwned(storedSessionId: string, owner: RuntimeSessionStateOwner): string | undefined {
    return super.get(ownedStoredKey(storedSessionId, owner))
  }

  override has(storedSessionId: string): boolean {
    return this.get(storedSessionId) !== undefined
  }

  override delete(storedSessionId: string): boolean {
    if (super.has(storedSessionId)) {
      return super.delete(storedSessionId)
    }

    const matches = [...super.keys()].filter(key => publicStoredId(key) === storedSessionId)

    return matches.length === 1 ? super.delete(matches[0]) : false
  }

  deleteAll(storedSessionId: string): number {
    const matches = [...super.keys()].filter(key => publicStoredId(key) === storedSessionId)

    for (const key of matches) {
      super.delete(key)
    }

    return matches.length
  }

  override *entries(): MapIterator<[string, string]> {
    for (const [key, runtimeId] of super.entries()) {
      yield [publicStoredId(key), runtimeId]
    }
  }

  override *keys(): MapIterator<string> {
    for (const key of super.keys()) {
      yield publicStoredId(key)
    }
  }

  override forEach(
    callbackfn: (value: string, key: string, map: Map<string, string>) => void,
    thisArg?: unknown
  ): void {
    for (const [storedSessionId, runtimeId] of this.entries()) {
      callbackfn.call(thisArg, runtimeId, storedSessionId, this)
    }
  }

  override [Symbol.iterator](): MapIterator<[string, string]> {
    return this.entries()
  }

  setOwned(storedSessionId: string, runtimeId: string, owner: RuntimeSessionStateOwner): this {
    return super.set(ownedStoredKey(storedSessionId, owner), runtimeId)
  }

  deleteOwned(storedSessionId: string, owner: RuntimeSessionStateOwner): boolean {
    return super.delete(ownedStoredKey(storedSessionId, owner))
  }
}

/** Runtime ids are source-local. Preserve legacy untagged-primary behavior,
 * but compare the composite registry scope whenever both sides name a source. */
export function sessionRuntimeStateMatchesOwner(
  state: ClientSessionState | undefined,
  owner: RuntimeSessionStateOwner
): state is ClientSessionState {
  if (!state?.profile || state.profile !== owner.profile) {
    return false
  }

  const stateConnectionId = state.connectionId?.trim() || null
  const ownerConnectionId = owner.connectionId?.trim() || null

  return (
    !stateConnectionId ||
    !ownerConnectionId ||
    registryBackendScopeKey(stateConnectionId, state.profile) ===
      registryBackendScopeKey(ownerConnectionId, owner.profile)
  )
}

/** Fail closed: unknown profile provenance never aliases the default owner. */
export function sessionStateMatchesOwner(
  state: ClientSessionState | undefined,
  owner: SessionStateOwner
): state is ClientSessionState {
  return sessionRuntimeStateMatchesOwner(state, owner) && state.storedSessionId === owner.storedSessionId
}

interface SessionStateCacheLimits {
  maxBytes?: number
  maxCount?: number
}

interface SessionStateCacheCallbacks {
  isReferenced: (runtimeId: string, state: ClientSessionState) => boolean
  onEvict: (runtimeId: string, state: ClientSessionState) => void
  /** Optional liveness check for a cached snapshot's in-flight claims. A
   *  connection death mid-turn orphans snapshots: the respawned backend
   *  re-mints runtime ids, so their frozen busy/awaitingResponse flags never
   *  receive a settling publish (#95189) and would pin megabytes of warm
   *  transcript per reconnect cycle until restart. When wired, those flags
   *  only block eviction while the authoritative store still claims work for
   *  the same runtime id; without the probe they always block. */
  isAuthoritativelyActive?: (runtimeId: string, state: ClientSessionState) => boolean
}

function transcriptBytes(state: ClientSessionState): number {
  if (state.messages.length === 0) {
    return 0
  }

  // JS strings occupy two bytes per UTF-16 code unit. JSON also accounts for
  // ids, part tags, tool payloads, attachment metadata, and error text without
  // retaining a second serialized copy in the cache.
  return JSON.stringify(state.messages).length * 2
}

function hasDraftOrInFlightMessage(state: ClientSessionState): boolean {
  return state.messages.some(message => message.pending === true)
}

/**
 * Runtime state map whose settled, unreferenced transcripts form a weighted
 * LRU. Live/visible states and unsaved drafts are outside both limits.
 */
export class SessionStateCache extends Map<string, ClientSessionState> {
  readonly #callbacks: SessionStateCacheCallbacks
  readonly #maxBytes: number
  readonly #maxCount: number
  readonly #recency = new Map<string, number>()
  #clock = 0

  constructor(callbacks: SessionStateCacheCallbacks, limits: SessionStateCacheLimits = {}) {
    super()
    this.#callbacks = callbacks
    this.#maxBytes = limits.maxBytes ?? DEFAULT_WARM_SESSION_TRANSCRIPT_BYTES
    this.#maxCount = limits.maxCount ?? DEFAULT_WARM_SESSION_TRANSCRIPT_COUNT
  }

  override get(runtimeId: string): ClientSessionState | undefined {
    const direct = super.get(runtimeId)

    const matches = direct
      ? [[runtimeId, direct] as const]
      : [...super.entries()].filter(([key]) => publicRuntimeId(key) === runtimeId)

    const found = matches.length === 1 ? matches[0] : undefined
    const state = found?.[1]

    if (state) {
      this.#touch(found![0])
    }

    return state
  }

  override set(runtimeId: string, state: ClientSessionState): this {
    const key = ownedRuntimeKey(runtimeId, {
      connectionId: state.connectionId,
      profile: state.profile ?? 'default'
    })

    super.set(key, state)
    this.#touch(key)

    return this
  }

  override delete(runtimeId: string): boolean {
    const direct = super.has(runtimeId)
      ? runtimeId
      : [...super.keys()].filter(key => publicRuntimeId(key) === runtimeId)[0]

    if (
      !direct ||
      (!super.has(runtimeId) && [...super.keys()].filter(key => publicRuntimeId(key) === runtimeId).length !== 1)
    ) {
      return false
    }

    this.#recency.delete(direct)

    return super.delete(direct)
  }

  getOwned(runtimeId: string, owner: RuntimeSessionStateOwner): ClientSessionState | undefined {
    const key = ownedRuntimeKey(runtimeId, owner)
    const state = super.get(key)

    if (state) {
      this.#touch(key)
    }

    return state
  }

  override clear(): void {
    this.#recency.clear()
    super.clear()
  }

  prune(): void {
    const candidates: Array<{ bytes: number; runtimeId: string; state: ClientSessionState; touched: number }> = []
    let bytes = 0

    for (const [stateKey, state] of this.entries()) {
      const runtimeId = publicRuntimeId(stateKey)

      if (!this.#isWarmSettled(runtimeId, state)) {
        continue
      }

      const weight = transcriptBytes(state)
      candidates.push({ bytes: weight, runtimeId: stateKey, state, touched: this.#recency.get(stateKey) ?? 0 })
      bytes += weight
    }

    let count = candidates.length

    if (count <= this.#maxCount && bytes <= this.#maxBytes) {
      return
    }

    candidates.sort((a, b) => a.touched - b.touched)

    for (const candidate of candidates) {
      if (count <= this.#maxCount && bytes <= this.#maxBytes) {
        break
      }

      // References and activity can change between insertion and pruning.
      const current = super.get(candidate.runtimeId)

      if (current !== candidate.state || !this.#isWarmSettled(candidate.runtimeId, current)) {
        continue
      }

      super.delete(candidate.runtimeId)
      this.#recency.delete(candidate.runtimeId)
      count -= 1
      bytes -= candidate.bytes
      this.#callbacks.onEvict(publicRuntimeId(candidate.runtimeId), candidate.state)
    }
  }

  #isWarmSettled(runtimeId: string, state: ClientSessionState): boolean {
    // In-flight claims pin a transcript only while they are trustworthy: with
    // an authority probe wired, a frozen busy/awaitingResponse on an orphaned
    // snapshot stops blocking eviction (see callback docs). Without one, the
    // legacy behavior holds and the flags always block.
    if (
      (state.busy || state.awaitingResponse) &&
      this.#callbacks.isAuthoritativelyActive?.(runtimeId, state) !== false
    ) {
      return false
    }

    return (
      Boolean(state.storedSessionId) &&
      state.messages.length > 0 &&
      !state.needsInput &&
      !hasDraftOrInFlightMessage(state) &&
      !this.#callbacks.isReferenced(runtimeId, state)
    )
  }

  #touch(runtimeId: string): void {
    this.#clock += 1
    this.#recency.set(runtimeId, this.#clock)
  }
}
