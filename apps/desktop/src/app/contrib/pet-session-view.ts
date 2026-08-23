import { normalizeProfileKey } from '@/store/profile'
import { sessionMatchesStoredId } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

export interface PetSessionViewInput {
  activeGatewayProfile: null | string | undefined
  activeSessionId: null | string
  currentView: string
  documentVisible: boolean
  profileReady: boolean
  routedSessionId: null | string
  resumeExhaustedSessionId: null | string
  resumeFailedSessionId: null | string
  runtimeIdByStoredSessionId: ReadonlyMap<string, string>
  selectedStoredSessionId: null | string
  sessions: readonly Pick<SessionInfo, '_lineage_root_id' | 'id' | 'profile'>[]
  windowFocused: boolean
}

export type PetSessionViewPayload = { sessionID: null } | { profile: string; sessionID: string }

/**
 * Resolve only a fully selected, loaded, profile-matched chat into the
 * content-free marker that Hermes Pet consumes. Invalid route/selection state
 * returns a clear marker. A valid but backgrounded document returns `null` so
 * the last foreground evidence is retained until the window is actually fronted.
 */
export function resolvePetSessionViewPayload(input: PetSessionViewInput): PetSessionViewPayload | null {
  const {
    activeGatewayProfile,
    activeSessionId,
    currentView,
    documentVisible,
    profileReady,
    routedSessionId,
    resumeExhaustedSessionId,
    resumeFailedSessionId,
    runtimeIdByStoredSessionId,
    selectedStoredSessionId,
    sessions,
    windowFocused
  } = input

  if (
    currentView !== 'chat' ||
    !profileReady ||
    !routedSessionId ||
    selectedStoredSessionId !== routedSessionId ||
    !activeSessionId ||
    resumeFailedSessionId === routedSessionId ||
    resumeExhaustedSessionId === routedSessionId
  ) {
    return { sessionID: null }
  }

  const stored = sessions.find(session => sessionMatchesStoredId(session, routedSessionId))

  if (!stored) {
    return { sessionID: null }
  }

  const profile = normalizeProfileKey(stored.profile)

  // A route may come from the all-profiles list while the gateway is still
  // re-homing. Never record a view under whichever profile happens to be live.
  if (profile !== normalizeProfileKey(activeGatewayProfile)) {
    return { sessionID: null }
  }

  // Pet's runtime snapshot identifies work by the backend's stored session key
  // (the same key passed to AIAgent/session hooks), not Desktop's ephemeral
  // renderer runtime id. During compression the durable key rotates; require
  // the active runtime's reverse mapping to have settled on the exact routed
  // key before publishing, otherwise a lineage root could acknowledge the
  // wrong completion.
  const runtimeStoredSessionId = [...runtimeIdByStoredSessionId.entries()].find(
    ([, runtimeId]) => runtimeId === activeSessionId
  )?.[0]

  if (runtimeStoredSessionId !== routedSessionId) {
    return { sessionID: null }
  }

  if (!documentVisible || !windowFocused) {
    return null
  }

  return { sessionID: routedSessionId, profile }
}
