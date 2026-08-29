/**
 * The window's ONE session-scoped RPC dispatcher, factored out of the contrib
 * wiring controller so the exact production routing (not a re-implementation)
 * can be driven by integration tests alongside the real session/prompt hooks.
 *
 * Route each RPC by the session IT targets, not by whatever tile is focused.
 * `requestGateway` is one shared closure used for every session RPC in the
 * window; keying the owner off $focusedStoredSessionId sent a NON-focused
 * tile's RPC (any bot chat while another pane is active) to the focused tile's
 * backend. That is the Bot Mode bug: a bot's prompt.submit carried its own
 * session_id but ran on the default backend (served via ?profile= from the
 * default's state.db), or 4001'd when the default backend didn't hold the
 * runtime session.
 *
 * params.session_id is a RUNTIME id, while tiles and session rows key on the
 * STORED id, so translate first (state cache, then a reverse scan of the
 * stored->runtime map, then the persisted tile map — the same ladder
 * use-session-tile-delegate uses, plus the tile rung that survives a reload
 * when the state cache is cold). A miss on ALL rungs means the id is already a
 * stored id (several RPCs pass stored ids directly), so use it as-is. Only an
 * RPC with no session_id at all (ambient/config calls) keeps the focused-tile
 * route.
 *
 * Session-scoped RPCs route to the backend that OWNS the session — never to
 * whatever is "active" (active is presentation only). The owner ladder is
 * resolveSessionRpcOwner (tile route → exact unique owner hint → the row's
 * owner: exact when connection-tagged, else its profile), then a
 * cross-profile REST probe for a hidden/unlisted session. A request with a
 * session whose owner STILL cannot be named fails closed with an explicit
 * SessionOwnerResolutionError rather than riding the ambient socket (the one
 * exception: the legacy single-backend Desktop, where ambient IS the owner).
 * Only a request with NO session at all falls to the ambient socket.
 */
import type { MutableRefObject } from 'react'

import { resolveSessionOwner } from '@/app/session/hooks/use-session-actions/utils'
import type { ClientSessionState } from '@/app/types'
import { getSessionOwnerHint, knownSessionOwner, ownerLookupSessionRows, requestSessionResume } from '@/store/session'
import { assertSessionOwnerResolved } from '@/store/session-owner-resolution'
import { requestForSessionProfile, type SessionOwnerScope } from '@/store/session-request-router'
import { $focusedStoredSessionId, sessionTileOwnerRoute, storedSessionIdForRuntimeId } from '@/store/session-states'

import { findStoredIdForRuntimeId, resolveRoutingSessionId, resolveSessionRpcOwner } from './wiring-routing'

export type AmbientGatewayRequest = <T>(
  method: string,
  params?: Record<string, unknown>,
  timeoutMs?: number,
  signal?: AbortSignal
) => Promise<T>

export interface SessionRpcDispatcherDeps {
  ambientRequest: AmbientGatewayRequest
  runtimeIdByStoredSessionIdRef: MutableRefObject<Map<string, string>>
  selectedStoredSessionIdRef: MutableRefObject<null | string>
  sessionStateByRuntimeIdRef: MutableRefObject<Map<string, ClientSessionState>>
}

/** Gateway code from tui_gateway.server._sess_nowait for a runtime that was
 * reaped after this renderer missed its disconnect/reclaim event. Prefer the
 * structured code: an unrelated RPC can legitimately mention the same words.
 * Older bridges sometimes lose the code, so retain the narrow message fallback
 * only when there is no numeric code at all. */
function isStaleRuntimeSessionError(error: unknown): boolean {
  const code =
    error && typeof error === 'object' && typeof (error as { code?: unknown }).code === 'number'
      ? (error as { code: number }).code
      : undefined

  if (code !== undefined) {
    return code === 4001
  }

  const message = error instanceof Error ? error.message : String(error ?? '')

  return /session not found/i.test(message)
}

export function createSessionRpcDispatcher(deps: SessionRpcDispatcherDeps): AmbientGatewayRequest {
  const { ambientRequest, runtimeIdByStoredSessionIdRef, selectedStoredSessionIdRef, sessionStateByRuntimeIdRef } = deps

  return async <T>(method: string, params?: Record<string, unknown>, timeoutMs?: number, signal?: AbortSignal) => {
    const paramSessionId = typeof params?.session_id === 'string' && params.session_id ? params.session_id : undefined

    const routingSessionId = resolveRoutingSessionId({
      focusedStoredSessionId: $focusedStoredSessionId.get(),
      paramSessionId,
      selectedStoredSessionId: selectedStoredSessionIdRef.current,
      storedIdForRuntime: runtimeId =>
        sessionStateByRuntimeIdRef.current.get(runtimeId)?.storedSessionId ??
        findStoredIdForRuntimeId(runtimeIdByStoredSessionIdRef.current, runtimeId) ??
        storedSessionIdForRuntimeId(runtimeId) ??
        undefined
    })

    let owner: SessionOwnerScope = resolveSessionRpcOwner({
      routingSessionId,
      sessionOwnerHint: storedSessionId => getSessionOwnerHint(storedSessionId),
      sessionRowOwner: storedSessionId => knownSessionOwner(ownerLookupSessionRows(), storedSessionId),
      tileOwnerRoute: sessionTileOwnerRoute
    })

    if (!owner && routingSessionId) {
      // Unknown owner for a REAL session: probe across profiles (REST, not the
      // gateway socket, so no recursion) rather than defaulting to active. A
      // hit stamps ownership on the row (exact when the row came back
      // connection-tagged); a miss leaves owner undefined.
      const probed = await resolveSessionOwner(routingSessionId)

      if (probed) {
        owner = probed
      }
    }

    // A request that names a session but whose owner nobody can name must not
    // ride the ambient socket: that turns missing metadata into a misleading
    // backend "session not found" on a backend that never held the runtime.
    assertSessionOwnerResolved(owner, { method, sessionId: paramSessionId ? routingSessionId : null })

    try {
      return await requestForSessionProfile<T>(owner, ambientRequest, method, params ?? {}, timeoutMs, signal)
    } catch (error) {
      // A renderer can miss session.reclaimed while its persistent WS is briefly
      // disconnected. The server then reaps the old runtime and any later
      // session-scoped RPC gets 4001, even though its stored transcript remains
      // resumable. Prompt actions already retry their own RPCs; this shared seam
      // covers the other callers (status, approvals, process controls) and wakes
      // the route-resume hook so the visible chat rebinds without an app restart.
      //
      // Deliberately do not retry the failing RPC here: it may be a destructive
      // action, and a fresh runtime binding is asynchronous. Preserve its error
      // contract and only request a recovery for the currently visible main
      // session. Tiles own their own recovery lifecycle, while background rows
      // must never steal the foreground route.
      if (
        // These two RPCs are the resume lifecycle itself. A warm rebind first
        // activates the cached runtime; on 4001 resumeSession drops that map
        // and falls through to durable session.resume. Re-triggering from the
        // activate failure would enqueue a second explicit resume request.
        method !== 'session.resume' &&
        method !== 'session.activate' &&
        paramSessionId &&
        routingSessionId &&
        routingSessionId === selectedStoredSessionIdRef.current &&
        isStaleRuntimeSessionError(error)
      ) {
        requestSessionResume(routingSessionId, typeof owner === 'object' && owner ? owner : undefined)
      }

      throw error
    }
  }
}
