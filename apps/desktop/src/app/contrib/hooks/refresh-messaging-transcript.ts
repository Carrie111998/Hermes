import { isMessagingSource } from '@/lib/session-source'
import { sessionMatchesStoredId } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

type MessagingSessionRow = Pick<SessionInfo, '_lineage_root_id' | 'id' | 'profile' | 'source'>

interface ResolveMessagingTranscriptTargetOptions {
  getRuntimeIdForStoredSession: (storedSessionId: string) => null | string
  isRuntimeBusy: (runtimeSessionId: string) => boolean
  messagingSessions: MessagingSessionRow[]
  selectedStoredSessionId: null | string
}

interface ResolveOpenMessagingStoredSessionIdsOptions {
  messagingSessions: MessagingSessionRow[]
  selectedStoredSessionId: null | string
  sessionTiles: ReadonlyArray<{ storedSessionId: string }>
}

export interface MessagingTranscriptTarget {
  profile?: null | string
  runtimeSessionId: string
  storedSessionId: string
}

/**
 * Returns messaging sessions currently open in either the primary chat or a
 * session tile. Tiles deliberately do not mutate the primary session atoms, so
 * deriving this list from selectedStoredSessionId alone silently excludes
 * tiled messaging transcripts from background refreshes.
 */
export function resolveOpenMessagingStoredSessionIds(
  options: ResolveOpenMessagingStoredSessionIdsOptions
): string[] {
  const openIds = [options.selectedStoredSessionId, ...options.sessionTiles.map(tile => tile.storedSessionId)]
  const result: string[] = []

  for (const storedSessionId of openIds) {
    if (!storedSessionId || result.includes(storedSessionId)) {
      continue
    }

    const stored = options.messagingSessions.find(session => sessionMatchesStoredId(session, storedSessionId))

    if (stored && isMessagingSource(stored.source)) {
      result.push(storedSessionId)
    }
  }

  return result
}

export function resolveMessagingTranscriptTarget(
  options: ResolveMessagingTranscriptTargetOptions
): MessagingTranscriptTarget | null {
  const storedSessionId = options.selectedStoredSessionId

  if (!storedSessionId) {
    return null
  }

  const stored = options.messagingSessions.find(session => sessionMatchesStoredId(session, storedSessionId))
  const runtimeSessionId = options.getRuntimeIdForStoredSession(storedSessionId)

  if (!stored || !isMessagingSource(stored.source) || !runtimeSessionId || options.isRuntimeBusy(runtimeSessionId)) {
    return null
  }

  return {
    profile: stored.profile,
    runtimeSessionId,
    storedSessionId
  }
}
