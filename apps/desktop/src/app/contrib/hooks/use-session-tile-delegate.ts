import { useEffect } from 'react'

import { getSessionMessages, PROMPT_SUBMIT_REQUEST_TIMEOUT_MS } from '@/hermes'
import { toChatMessages } from '@/lib/chat-messages'
import { locateSessionTile, publishSessionState, setSessionTileDelegate } from '@/store/session-states'
import type { SessionResumeResponse } from '@/types/hermes'

import type { usePromptActions } from '../../session/hooks/use-prompt-actions'
import { resolveSessionProfile } from '../../session/hooks/use-session-actions/utils'
import type { useSessionStateCache } from '../../session/hooks/use-session-state-cache'
import type { GatewayRequester } from '../types'

type SessionStateCache = ReturnType<typeof useSessionStateCache>

interface SessionTileDelegateParams {
  archiveSession: (storedSessionId: string, profile?: string) => Promise<unknown>
  branchStoredSession: (storedSessionId: string, profile?: string) => Promise<unknown>
  executeSlashCommand: ReturnType<typeof usePromptActions>['executeSlashCommand']
  removeSession: (storedSessionId: string, profile?: string) => Promise<unknown>
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
      archiveSession: async (storedSessionId, profile) => {
        await archiveSession(storedSessionId, profile)
      },
      branchSession: async (storedSessionId, profile) => {
        await branchStoredSession(storedSessionId, profile)
      },
      deleteSession: async (storedSessionId, profile) => {
        await removeSession(storedSessionId, profile)
      },
      executeSlash: async (rawCommand, sessionId) => {
        await executeSlashCommand(rawCommand, { sessionId })
      },
      interruptSession: async runtimeId => {
        await requestGateway('session.interrupt', { session_id: runtimeId })
      },
      resumeTile: async (storedSessionId, ownerProfile) => {
        // Capture the visible tile's persistence bucket before the first await.
        // Profile switches replace `$sessionTiles`, and stored ids can collide
        // across buckets, so delayed owner backfills must not use the bucket
        // that happens to be active when resolution finishes.
        const tileLocation = locateSessionTile(storedSessionId)
        const explicitProfile = ownerProfile?.trim()

        // A quick-entry target has no durable tile identity, so retain the
        // existing warm shortcut there. A tile does: even a legacy ownerless
        // tile must resolve its profile before trusting an id-only cache,
        // because stored ids can collide across independent profile DBs.
        const existing =
          !tileLocation && !explicitProfile ? runtimeIdByStoredSessionIdRef.current.get(storedSessionId) : undefined

        const cached = existing ? sessionStateByRuntimeIdRef.current.get(existing) : undefined

        if (existing && cached?.storedSessionId === storedSessionId) {
          publishSessionState(existing, cached)

          return { runtimeId: existing }
        }

        // Resolve the owning profile before binding a runtime. A tile can open a
        // session from any profile, not just the active one; resuming (or
        // reading messages) without a profile lets the gateway fall back to the
        // launch-profile DB and fork the conversation into the wrong profile —
        // the same cross-profile bleed the recovery resumes had (#67603).
        const profile = explicitProfile || (await resolveSessionProfile(storedSessionId))

        // Older v2 tile records predate durable ownership. Return the resolved
        // owner with the runtime so SessionTilePane can publish both in one
        // bucket/owner compare-and-set. That atomic handoff prevents a delayed
        // resume from rebinding a same-id tile retargeted to another profile.
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

        updateSessionState(
          runtimeId,
          state => ({
            ...state,
            busy: Boolean(resumed?.info?.running),
            messages:
              state.messages.length > 0 ? state.messages : toChatMessages(prefetch?.messages ?? resumed?.messages ?? [])
          }),
          storedSessionId
        )

        return { profile: profile || undefined, runtimeId }
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
