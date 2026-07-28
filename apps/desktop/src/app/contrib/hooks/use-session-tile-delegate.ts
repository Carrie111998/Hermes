import { useEffect } from 'react'

import { getSessionMessages, PROMPT_SUBMIT_REQUEST_TIMEOUT_MS } from '@/hermes'
import { toChatMessages } from '@/lib/chat-messages'
import { sessionIdentityKey } from '@/lib/session-identity'
import { $activeGatewayProfile, normalizeProfileKey } from '@/store/profile'
import { publishSessionState, setSessionTileDelegate } from '@/store/session-states'
import type { SessionResumeResponse } from '@/types/hermes'

import type { usePromptActions } from '../../session/hooks/use-prompt-actions'
import { resolveSessionProfile } from '../../session/hooks/use-session-actions/utils'
import type { useSessionStateCache } from '../../session/hooks/use-session-state-cache'
import type { GatewayRequester, ProfileGatewayRequester } from '../types'

type SessionStateCache = ReturnType<typeof useSessionStateCache>

interface SessionTileDelegateParams {
  archiveSession: (storedSessionId: string, profile?: null | string) => Promise<unknown>
  branchStoredSession: (storedSessionId: string, profile?: null | string) => Promise<unknown>
  executeSlashCommand: ReturnType<typeof usePromptActions>['executeSlashCommand']
  removeSession: (storedSessionId: string, profile?: null | string) => Promise<unknown>
  requestGateway: GatewayRequester
  requestGatewayForProfile?: ProfileGatewayRequester
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
  requestGatewayForProfile,
  runtimeIdByStoredSessionIdRef,
  sessionStateByRuntimeIdRef,
  updateSessionState
}: SessionTileDelegateParams): void {
  useEffect(() => {
    const requestOwnedGateway: ProfileGatewayRequester = requestGatewayForProfile
      ? requestGatewayForProfile
      : (_profile, method, params, timeoutMs) =>
          timeoutMs === undefined ? requestGateway(method, params) : requestGateway(method, params, timeoutMs)

    setSessionTileDelegate({
      archiveSession: async (storedSessionId, ownerProfile) => {
        const profile = normalizeProfileKey(ownerProfile ?? $activeGatewayProfile.get())
        await archiveSession(storedSessionId, profile)
      },
      branchSession: async (storedSessionId, ownerProfile) => {
        const profile = normalizeProfileKey(ownerProfile ?? $activeGatewayProfile.get())
        await branchStoredSession(storedSessionId, profile)
      },
      deleteSession: async (storedSessionId, ownerProfile) => {
        const profile = normalizeProfileKey(ownerProfile ?? $activeGatewayProfile.get())
        await removeSession(storedSessionId, profile)
      },
      executeSlash: async (rawCommand, sessionId, profile, storedSessionId) => {
        await executeSlashCommand(rawCommand, { profile, sessionId, storedSessionId })
      },
      interruptSession: async (runtimeId, ownerProfile) => {
        const profile = normalizeProfileKey(ownerProfile)
        await requestOwnedGateway(profile, 'session.interrupt', { session_id: runtimeId })
      },
      resumeTile: async (storedSessionId, ownerProfile) => {
        const profile = normalizeProfileKey(
          ownerProfile ?? (await resolveSessionProfile(storedSessionId)) ?? $activeGatewayProfile.get()
        )

        const existing = runtimeIdByStoredSessionIdRef.current.get(sessionIdentityKey(storedSessionId, profile))
        const cached = existing ? sessionStateByRuntimeIdRef.current.get(existing) : undefined

        if (
          existing &&
          cached?.storedSessionId === storedSessionId &&
          normalizeProfileKey(cached.storedSessionProfile) === profile
        ) {
          publishSessionState(existing, cached)

          return existing
        }

        const [prefetch, resumed] = await Promise.all([
          getSessionMessages(storedSessionId, profile).catch(() => null),
          requestOwnedGateway<SessionResumeResponse>(profile, 'session.resume', {
            session_id: storedSessionId,
            cols: 96,
            profile
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
          storedSessionId,
          profile
        )

        return runtimeId
      },
      submitToSession: async (runtimeId, text, ownerProfile) => {
        const profile = normalizeProfileKey(ownerProfile)
        await requestOwnedGateway(
          profile,
          'prompt.submit',
          { session_id: runtimeId, text },
          PROMPT_SUBMIT_REQUEST_TIMEOUT_MS
        )
      },
      updateSession: (runtimeId, updater) => updateSessionState(runtimeId, updater)
    })
  }, [
    archiveSession,
    branchStoredSession,
    executeSlashCommand,
    removeSession,
    requestGateway,
    requestGatewayForProfile,
    runtimeIdByStoredSessionIdRef,
    sessionStateByRuntimeIdRef,
    updateSessionState
  ])
}
