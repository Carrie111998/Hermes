import { isMessagingSource } from '@/lib/session-source'
import { sessionMatchesStoredId, sessionPinId } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

type MessagingSessionRow = Pick<SessionInfo, '_lineage_root_id' | 'id' | 'profile' | 'source'>

interface ResolveOpenMessagingCandidateIdsOptions {
  knownSessions: MessagingSessionRow[]
  messagingSessions: MessagingSessionRow[]
  profile: string
  selectedStoredSessionId: null | string
  sessionTiles: ReadonlyArray<{ storedSessionId: string }>
}

interface ResolveOpenTranscriptSurfacesOptions {
  activeRuntimeSessionId: null | string
  profile: string
  selectedStoredSessionId: null | string
  sessionTiles: ReadonlyArray<{ runtimeId?: null | string; storedSessionId: string }>
}

export interface OpenTranscriptSurface {
  runtimeSessionId: string
  storedSessionId: string
  profile: string
}

interface IsTranscriptRefreshScopeCurrentOptions {
  activeProfile: null | string | undefined
  capturedProfileEpoch: number
  currentProfileEpoch: number
  profile: null | string | undefined
  runtimeSessionId: string
  surfaces: OpenTranscriptSurface[]
}

interface ResolveProfileScopedStoredSessionOptions {
  getSession: (storedSessionId: string, profile: string) => Promise<MessagingSessionRow>
  profile: string
  storedSessionId: string
}

export function resolveAuthoritativeRuntimeState<TState>(
  runtimeSessionId: string,
  publishedStates: Readonly<Record<string, TState | undefined>>,
  cachedStates: ReadonlyMap<string, TState>
): TState | undefined {
  return publishedStates[runtimeSessionId] ?? cachedStates.get(runtimeSessionId)
}

export interface MessagingTranscriptRuntimeState {
  busy: boolean
  storedSessionId: null | string
}

export interface ResolvedMessagingTranscriptTarget {
  key: string
  profile?: null | string
  runtimeSessionIds: string[]
  session: MessagingSessionRow
  storedSessionId: string
}

interface ResolveMessagingTranscriptTargetsOptions {
  getRuntimeState: (runtimeSessionId: string) => MessagingTranscriptRuntimeState | undefined
  resolveStoredSession: (storedSessionId: string, profile: string) => Promise<MessagingSessionRow | undefined>
  sessionRows: MessagingSessionRow[]
  surfaces: OpenTranscriptSurface[]
}

const profileKey = (profile: null | string | undefined) => profile?.trim() || 'default'

export async function resolveProfileScopedStoredSession(
  options: ResolveProfileScopedStoredSessionOptions
): Promise<MessagingSessionRow> {
  const profile = profileKey(options.profile)

  return {
    ...(await options.getSession(options.storedSessionId, profile)),
    profile
  }
}

export function isTranscriptRefreshScopeCurrent(
  options: IsTranscriptRefreshScopeCurrentOptions
): boolean {
  if (options.capturedProfileEpoch !== options.currentProfileEpoch) {
    return false
  }

  const profile = profileKey(options.profile)

  if (profileKey(options.activeProfile) !== profile) {
    return false
  }

  return options.surfaces.some(
    surface => surface.runtimeSessionId === options.runtimeSessionId && surface.profile === profile
  )
}

const findSessionInProfile = (sessions: MessagingSessionRow[], storedSessionId: string, profile: string) =>
  sessions.find(
    session => profileKey(session.profile) === profile && sessionMatchesStoredId(session, storedSessionId)
  )

interface RefreshMessagingTranscriptTargetOptions<TTranscript> {
  commit: (
    runtimeSessionId: string,
    currentState: MessagingTranscriptRuntimeState,
    transcript: TTranscript
  ) => void
  generationByTarget: Map<string, number>
  getCurrentRuntimeState: (runtimeSessionId: string) => MessagingTranscriptRuntimeState | undefined
  getSignature: (transcript: TTranscript) => string
  isRuntimeOpen: (runtimeSessionId: string) => boolean
  loadTranscript: () => Promise<TTranscript>
  signatureByRuntimeId: Map<string, string>
  target: ResolvedMessagingTranscriptTarget
}

export function resolveOpenTranscriptSurfaces(
  options: ResolveOpenTranscriptSurfacesOptions
): OpenTranscriptSurface[] {
  const surfaces: OpenTranscriptSurface[] = []
  const runtimeIds = new Set<string>()
  const profile = profileKey(options.profile)

  const add = (runtimeSessionId: null | string | undefined, storedSessionId: null | string) => {
    if (!runtimeSessionId || !storedSessionId || runtimeIds.has(runtimeSessionId)) {
      return
    }

    runtimeIds.add(runtimeSessionId)
    surfaces.push({ profile, runtimeSessionId, storedSessionId })
  }

  add(options.activeRuntimeSessionId, options.selectedStoredSessionId)

  for (const tile of options.sessionTiles) {
    add(tile.runtimeId, tile.storedSessionId)
  }

  return surfaces
}

/**
 * Returns known messaging sessions plus unresolved open candidates from both
 * the primary surface and independent session tiles. Exact metadata resolution
 * later filters non-messaging candidates without treating the capped sidebar
 * slice as authoritative.
 */
export function resolveOpenMessagingCandidateIds(
  options: ResolveOpenMessagingCandidateIdsOptions
): string[] {
  const openIds = [options.selectedStoredSessionId, ...options.sessionTiles.map(tile => tile.storedSessionId)]
  const profile = profileKey(options.profile)
  const result = new Set<string>()

  for (const storedSessionId of openIds) {
    if (!storedSessionId) {
      continue
    }

    const stored =
      findSessionInProfile(options.messagingSessions, storedSessionId, profile) ??
      findSessionInProfile(options.knownSessions, storedSessionId, profile)

    if (stored && isMessagingSource(stored.source)) {
      // A durable tile may still carry the lineage root after compression while
      // the fresh sidebar row carries the live continuation tip. Normalize to
      // the live id so root/tip aliases do not schedule duplicate reads.
      result.add(stored.id)
    } else if (!stored) {
      // The messaging sidebar is a capped page. Keep unknown open surfaces as
      // candidates so the caller can resolve their metadata by exact id rather
      // than treating absence from that page as proof they are local sessions.
      result.add(storedSessionId)
    }
  }

  return [...result]
}

/**
 * Resolves the open renderer surfaces to transport-agnostic messaging targets.
 * A surface owns its runtime id directly; the durable stored id is only the
 * lookup fallback because compression may already have rotated the live state
 * to a continuation tip. Unknown ids are resolved exactly instead of being
 * discarded merely because the messaging sidebar's capped page omitted them.
 */
export async function resolveMessagingTranscriptTargets(
  options: ResolveMessagingTranscriptTargetsOptions
): Promise<ResolvedMessagingTranscriptTarget[]> {
  const grouped = new Map<string, ResolvedMessagingTranscriptTarget>()

  const resolveForProfile = async (storedSessionId: string, profile: string) => {
    try {
      const resolved = await options.resolveStoredSession(storedSessionId, profile)

      return resolved && profileKey(resolved.profile) === profile ? resolved : undefined
    } catch {
      return undefined
    }
  }

  for (const surface of options.surfaces) {
    const profile = profileKey(surface.profile)
    const runtimeState = options.getRuntimeState(surface.runtimeSessionId)

    if (!runtimeState || runtimeState.busy) {
      continue
    }

    const liveStoredSessionId = runtimeState.storedSessionId ?? surface.storedSessionId

    let stored = findSessionInProfile(options.sessionRows, liveStoredSessionId, profile)

    stored ??= await resolveForProfile(liveStoredSessionId, profile)

    if (!stored && liveStoredSessionId !== surface.storedSessionId) {
      stored =
        findSessionInProfile(options.sessionRows, surface.storedSessionId, profile) ??
        (await resolveForProfile(surface.storedSessionId, profile))
    }

    if (!stored || !isMessagingSource(stored.source)) {
      continue
    }

    const lineageRoot = sessionPinId(stored)
    const key = `${profile}:${lineageRoot}`
    const existing = grouped.get(key)

    if (existing) {
      if (!existing.runtimeSessionIds.includes(surface.runtimeSessionId)) {
        existing.runtimeSessionIds.push(surface.runtimeSessionId)
      }

      // Prefer a live continuation tip over a durable lineage root when two
      // aliases for the same transcript are simultaneously open.
      if (existing.storedSessionId === lineageRoot && stored.id !== lineageRoot) {
        existing.session = stored
        existing.storedSessionId = stored.id
      }

      continue
    }

    grouped.set(key, {
      key,
      profile,
      runtimeSessionIds: [surface.runtimeSessionId],
      session: stored,
      storedSessionId: stored.id
    })
  }

  return [...grouped.values()]
}

/**
 * Loads one canonical transcript and commits it to every still-open renderer
 * runtime for that lineage. Generation, membership, identity, and authoritative
 * busy state are rechecked after the await so a slow poll cannot overwrite a
 * newer response, a running turn, a closed tile, or a rebound runtime.
 */
export async function refreshMessagingTranscriptTarget<TTranscript>(
  options: RefreshMessagingTranscriptTargetOptions<TTranscript>
): Promise<void> {
  const hasIdleRuntime = options.target.runtimeSessionIds.some(runtimeSessionId => {
    const state = options.getCurrentRuntimeState(runtimeSessionId)

    return Boolean(
      state &&
        !state.busy &&
        sessionMatchesStoredId(
          options.target.session,
          state.storedSessionId ?? options.target.storedSessionId
        )
    )
  })

  if (!hasIdleRuntime) {
    return
  }

  const generation = (options.generationByTarget.get(options.target.key) ?? 0) + 1
  options.generationByTarget.set(options.target.key, generation)

  const transcript = await options.loadTranscript()

  if (options.generationByTarget.get(options.target.key) !== generation) {
    return
  }

  const signature = options.getSignature(transcript)

  for (const runtimeSessionId of options.target.runtimeSessionIds) {
    const currentState = options.getCurrentRuntimeState(runtimeSessionId)
    const signatureKey = `${options.target.key}:${runtimeSessionId}`

    if (
      !currentState ||
      currentState.busy ||
      !options.isRuntimeOpen(runtimeSessionId) ||
      !sessionMatchesStoredId(
        options.target.session,
        currentState.storedSessionId ?? options.target.storedSessionId
      ) ||
      options.signatureByRuntimeId.get(signatureKey) === signature
    ) {
      continue
    }

    options.commit(runtimeSessionId, currentState, transcript)
    options.signatureByRuntimeId.set(signatureKey, signature)
  }
}
