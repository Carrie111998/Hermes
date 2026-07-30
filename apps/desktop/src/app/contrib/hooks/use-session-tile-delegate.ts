import { useEffect } from 'react'

import { getSessionMessages, PROMPT_SUBMIT_REQUEST_TIMEOUT_MS } from '@/hermes'
import { toChatMessages } from '@/lib/chat-messages'
import { publishSessionState, setSessionTileDelegate } from '@/store/session-states'
import type { SessionResumeResponse } from '@/types/hermes'

import type { usePromptActions } from '../../session/hooks/use-prompt-actions'
import { resolveSessionProfile } from '../../session/hooks/use-session-actions/utils'
import type { useSessionStateCache } from '../../session/hooks/use-session-state-cache'
import type { GatewayRequester } from '../types'

type SessionStateCache = ReturnType<typeof useSessionStateCache>

interface SessionTileDelegateParams {
  archiveSession: (storedSessionId: string) => Promise<unknown>
  branchStoredSession: (storedSessionId: string) => Promise<unknown>
  executeSlashCommand: ReturnType<typeof usePromptActions>['executeSlashCommand']
  removeSession: (storedSessionId: string) => Promise<unknown>
  requestGateway: GatewayRequester
  runtimeIdByStoredSessionIdRef: SessionStateCache['runtimeIdByStoredSessionIdRef']
  sessionStateByRuntimeIdRef: SessionStateCache['sessionStateByRuntimeIdRef']
  updateSessionState: SessionStateCache['updateSessionState']
}

/**
 * Publishes the session-tile delegate: resume / submit / interrupt / slash for
 * tiled sessions WITHOUT touching the primary view ($activeSessionId /
 * $messages stay the main thread's). Resume reuses a live runtime binding when
 * one exists (incl. the main thread's own session); a cold tile binds +
 * hydrates the cache, which publishSessionState mirrors to the tile.
 */
export function useSessionTileDelegate({
  archiveSession,
  branchStoredSession,
  executeSlashCommand,
  removeSession,
  requestGateway,
  runtimeIdByStoredSessionIdRef,
  sessionStateByRuntimeIdRef,
  updateSessionState
}: SessionTileDelegateParams): void {
  useEffect(() => {
    setSessionTileDelegate({
      archiveSession: async storedSessionId => {
        await archiveSession(storedSessionId)
      },
      branchSession: async storedSessionId => {
        await branchStoredSession(storedSessionId)
      },
      deleteSession: async storedSessionId => {
        await removeSession(storedSessionId)
      },
      executeSlash: async (rawCommand, sessionId) => {
        await executeSlashCommand(rawCommand, { sessionId })
      },
      interruptSession: async runtimeId => {
        await requestGateway('session.interrupt', { session_id: runtimeId })
      },
      resumeTile: async (storedSessionId, requestedProfile) => {
        const explicitProfile = requestedProfile?.trim() || null
        const existing = runtimeIdByStoredSessionIdRef.current.get(storedSessionId)
        const cached = existing ? sessionStateByRuntimeIdRef.current.get(existing) : undefined
        const previousMappedRuntimeId = cached?.storedSessionId === storedSessionId ? existing : undefined

        // An explicit profile is authoritative and must not reuse the global
        // stored-id cache: different profile DBs can theoretically contain the
        // same stored id, and the caller supplied the ownership precisely to
        // avoid cross-profile inference.
        if (!explicitProfile && existing && cached?.storedSessionId === storedSessionId) {
          publishSessionState(existing, cached)

          return existing
        }

        // Resolve the owning profile before binding a runtime. A tile can open a
        // session from any profile, not just the active one; resuming (or
        // reading messages) without a profile lets the gateway fall back to the
        // launch-profile DB and fork the conversation into the wrong profile —
        // the same cross-profile bleed the recovery resumes had (#67603).
        const profile = explicitProfile || (await resolveSessionProfile(storedSessionId))

        const [prefetch, resumed] = await Promise.all([
          getSessionMessages(storedSessionId, profile).catch(() => null),
          requestGateway<SessionResumeResponse>('session.resume', {
            session_id: storedSessionId,
            cols: 96,
            ...(profile ? { profile } : {})
          })
        ])

        const runtimeId = resumed?.session_id

        if (!runtimeId) {
          throw new Error('resume returned no session id')
        }

        try {
          updateSessionState(
            runtimeId,
            state => ({
              ...state,
              busy: Boolean(resumed?.info?.running),
              messages:
                state.messages.length > 0
                  ? state.messages
                  : toChatMessages(prefetch?.messages ?? resumed?.messages ?? [])
            }),
            storedSessionId
          )
        } finally {
          if (explicitProfile) {
            // updateSessionState normally registers a profile-agnostic
            // stored-id → runtime mapping. An authoritative-profile background
            // resume must not replace another profile's valid mapping (or leave
            // its own behind), because later ordinary tile resumes do not carry
            // enough ownership information to distinguish colliding ids.
            if (previousMappedRuntimeId) {
              runtimeIdByStoredSessionIdRef.current.set(storedSessionId, previousMappedRuntimeId)
            } else {
              runtimeIdByStoredSessionIdRef.current.delete(storedSessionId)
            }
          }
        }

        return runtimeId
      },
      submitToSession: async (runtimeId, text) => {
        await requestGateway('prompt.submit', { session_id: runtimeId, text }, PROMPT_SUBMIT_REQUEST_TIMEOUT_MS)
      },
      updateSession: (runtimeId, updater) => updateSessionState(runtimeId, updater)
    })
  }, [
    archiveSession,
    branchStoredSession,
    executeSlashCommand,
    removeSession,
    requestGateway,
    runtimeIdByStoredSessionIdRef,
    sessionStateByRuntimeIdRef,
    updateSessionState
  ])
}
