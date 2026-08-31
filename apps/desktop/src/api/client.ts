import { JsonRpcGatewayClient } from '@hermes/shared'

import type { HermesApiRequest } from '@/global'
import { browserSpectatorApi } from '@/platform/browser-spectator'

// Desktop startup fires a burst of read-only data calls (config, profiles,
// model info/options, cron) the moment the backend passes readiness. On a
// profile-heavy or remote install these can each take tens of seconds — e.g.
// /api/profiles runs list_profiles(), which does a recursive skill-tree walk
// per profile — so the 15s default (DEFAULT_FETCH_TIMEOUT_MS in hardening.ts)
// times out a backend that is alive-but-busy, surfacing as a spurious
// "Timed out connecting to Hermes backend" that hangs the UI (#48504).
//
// Give the boot burst a generous per-call timeout instead of raising the
// global default: interactive/runtime calls and the liveness poll (/api/status)
// keep the short default so a genuinely-dead backend is still detected fast.
export const STARTUP_REQUEST_TIMEOUT_MS = 60_000
const DEFAULT_GATEWAY_REQUEST_TIMEOUT_MS = 30_000
// prompt.submit is effectively fire-and-forget: turn completion is signaled by
// stream / message.complete events, NOT by the RPC return. A long turn (MoA
// presets running references + aggregator in series, deep reasoning, large tool
// chains) can legitimately take minutes to ACK, so bounding the ack by the
// generic 30s default surfaces a false "request timed out" toast while the turn
// is still running and will succeed (issue #55024). Match the backend's
// agent-turn ceiling (agent.gateway_timeout = 1800s) so the ack timeout only
// ever fires when the turn itself would have been abandoned server-side.
export const PROMPT_SUBMIT_REQUEST_TIMEOUT_MS = 1_800_000

export class HermesGateway extends JsonRpcGatewayClient {
  constructor() {
    super({
      closedErrorMessage: 'Hermes gateway connection closed',
      connectErrorMessage: 'Could not connect to Hermes gateway',
      createRequestId: nextId => nextId,
      notConnectedErrorMessage: 'Hermes gateway is not connected',
      requestTimeoutMs: DEFAULT_GATEWAY_REQUEST_TIMEOUT_MS
    })
  }
}

interface SpectatorCursor {
  event_id: number
  runtime_generation: string
}

interface SpectatorSubscriptionResult {
  cursor: SpectatorCursor
  replay_gap: boolean
  runtime_generation: string
  session_id: string
  session_key: string
  subscribed: boolean
}

const SPECTATOR_RPC_METHODS = new Set(['session.subscribe', 'session.unsubscribe'])

/** Receive-only gateway used by the browser spectator. The server and browser
 * both allow exactly subscribe/unsubscribe; every writer/mutation RPC remains
 * fail-closed. Reconnect cursors stay in memory only and are scoped by runtime
 * generation, so a dropped socket replays missed events without persisting
 * credentials or treating replay as a durable transcript. */
export class HermesSpectatorGateway extends HermesGateway {
  private activeSubscription: null | string = null
  private readonly cursors = new Map<string, SpectatorCursor>()
  private readonly runtimeTargets = new Map<string, string>()
  private subscriptionQueue: Promise<void> = Promise.resolve()

  constructor() {
    super()
    this.onEvent(event => {
      const target = event.session_id ? this.runtimeTargets.get(event.session_id) : null

      if (
        target &&
        typeof event.event_id === 'number' &&
        Number.isInteger(event.event_id) &&
        event.event_id >= 0 &&
        typeof event.runtime_generation === 'string' &&
        event.runtime_generation
      ) {
        this.cursors.set(target, {
          event_id: event.event_id,
          runtime_generation: event.runtime_generation
        })
      }
    })
  }

  override request<T>(
    method: string,
    params: Record<string, unknown> = {},
    timeoutMs?: number,
    signal?: AbortSignal
  ): Promise<T> {
    if (!SPECTATOR_RPC_METHODS.has(method)) {
      return Promise.reject(new Error(`Hermes iPad spectator is read-only: ${method}`))
    }

    const operation = () =>
      method === 'session.subscribe'
        ? this.subscribe<T>(params, timeoutMs, signal)
        : this.unsubscribe<T>(params, timeoutMs, signal)

    const result = this.subscriptionQueue.then(operation, operation)

    this.subscriptionQueue = result.then(
      () => undefined,
      () => undefined
    )

    return result
  }

  private async subscribe<T>(params: Record<string, unknown>, timeoutMs?: number, signal?: AbortSignal): Promise<T> {
    const target = String(params.session_id ?? '').trim()

    if (!target) {
      throw new Error('session.subscribe requires session_id')
    }

    if (this.activeSubscription && this.activeSubscription !== target) {
      await super
        .request('session.unsubscribe', { session_id: this.activeSubscription }, timeoutMs, signal)
        .catch(() => undefined)
      this.activeSubscription = null
    }

    const cleanParams = { ...params }

    delete cleanParams.after_event_id
    delete cleanParams.cursor
    delete cleanParams.runtime_generation

    const cursor = this.cursors.get(target)
    const requestParams = cursor ? { ...cleanParams, cursor } : cleanParams

    let result = await super.request<SpectatorSubscriptionResult>('session.subscribe', requestParams, timeoutMs, signal)

    // The session-selection path hydrates the durable REST transcript before
    // it calls subscribe. A gap therefore means that hydration is already the
    // authoritative baseline: clear the stale cursor and attach at "now".
    if (result.replay_gap && !result.subscribed) {
      this.cursors.delete(target)
      result = await super.request<SpectatorSubscriptionResult>('session.subscribe', cleanParams, timeoutMs, signal)
    }

    if (result.subscribed) {
      this.activeSubscription = target
      this.runtimeTargets.set(result.session_id, target)
      this.cursors.set(target, result.cursor)
    }

    return result as T
  }

  private async unsubscribe<T>(params: Record<string, unknown>, timeoutMs?: number, signal?: AbortSignal): Promise<T> {
    const result = await super.request<T>('session.unsubscribe', params, timeoutMs, signal)
    const target = String(params.session_id ?? '').trim()

    if (!target || target === this.activeSubscription) {
      this.activeSubscription = null
    }

    return result
  }
}
// Profile that profile-scoped REST settings (config/env/skills/tools/model/…)
// should target. Mirrors $activeGatewayProfile, pushed in from the store via
// setApiRequestProfile so this module needs no store import (avoids a cycle).
// Electron main consumes request.profile as request scope. Local calls whose
// REST handlers accept profile reuse the primary dashboard via ?profile=;
// unscoped handlers retain a profile backend. Remote overrides still route to
// their owning backend. Null → primary, so single-profile users are unaffected.
let _apiProfile: null | string = null

export function setApiRequestProfile(profile: null | string): void {
  _apiProfile = profile || null
}

export function profileScoped(profile?: null | string): { profile?: string } {
  const selected = profile === undefined ? _apiProfile : profile

  return selected ? { profile: selected } : {}
}

/** Profile that profile-scoped REST/WS calls should target (null → primary).
 *  Read-only twin of setApiRequestProfile for modules (e.g. voice playback)
 *  that build their own connection URLs and must stay on the same backend. */
export function getApiRequestProfile(): null | string {
  return _apiProfile
}

// Registry connection serving the active gateway (null → the local pool).
// Pushed from store/gateway's setActive — the single seam BOTH
// ensureGatewayProfile and ensureGatewayAgent funnel through — so WS calls
// that dial their own backend (pluginSocket) resolve it through the SAME
// source of truth those paths maintain for $connection. That makes the plugin
// socket follow registry-agent activations too, not just profile switches.
// Same no-store-import contract as _apiProfile (avoids a cycle).
let _apiConnectionId: null | string = null

export function setApiRequestConnection(connectionId: null | string): void {
  _apiConnectionId = connectionId || null
}

// Registry connection scope for a REST request. A registered remote gateway
// owns its own state.db — cron jobs and their run sessions live THERE — so
// requests for gateway-owned data must carry the connection id for the main
// process to route them to that host (hermes:api's registry branch). Null
// resolves to no tag, keeping single-source users byte-identical; explicit
// 'local' must remain tagged when the legacy primary points elsewhere.
export function connectionScoped(): { connectionId?: string } {
  return _apiConnectionId ? { connectionId: _apiConnectionId } : {}
}

/** Send a REST request to the renderer's active registry source. Request-level
 *  routing may override the active source for an explicitly-owned resource.
 *
 *  Helpers under `api/` go through here rather than calling the preload bridge
 *  directly, so the connection tag cannot be forgotten on a new one.
 *  capabilityScoped() now emits an explicit `connectionId` for EVERY object
 *  pin — `'local'` included — so a pin always overrides the ambient tag spread
 *  underneath it. (It used to omit the key for 'local', which made the pin
 *  unable to beat the ambient tag; helpers then had to bypass this wrapper.) */
export function hermesApi<T>(request: HermesApiRequest): Promise<T> {
  if (!window.hermesDesktop?.api) {
    return browserSpectatorApi<T>({ ...connectionScoped(), ...request })
  }

  return window.hermesDesktop.api<T>({ ...connectionScoped(), ...request })
}

// ── Capability scope: (connection, profile) routing for the Capabilities
// surface (skills / toolsets / MCP / hub / env / toolset config) ────────────
//
// A profile is not a machine-global name — it belongs to ONE gateway. The
// Capabilities surface can be pointed at any (connection, profile) pair
// (SkillsView's scope selector, Bot Mode's fixedProfile/fixedConnection), so
// its REST helpers accept either the legacy string form or an explicit scope
// object:
//
//   - `undefined` / string → the legacy profile path, PLUS the active registry
//     connection tag (connectionScoped, same contract the cron helpers adopted
//     in #87882). Without the tag, a window activated onto a registered remote
//     gateway read the LOCAL pool's skills/tools/MCP — the wrong machine.
//   - `{ connectionId, profile }` → explicit pin. A non-empty connection id —
//     `'local'` INCLUDED — is sent through so Electron's registry resolver
//     owns the routing. Dropping the `'local'` pin (the pre-#91564 behavior,
//     when an absent id always meant the local pool) silently re-routes a
//     "This device" pick to the registry PRIMARY once that primary is a
//     remote/cloud/ssh gateway: the v1 fallback route treats a remote registry
//     primary as global-remote, so the explicit pin is the ONLY way back to
//     this machine (see apiRequestRegistryConnectionId in Electron main).
export type ProfileScope = undefined | null | string | { connectionId?: null | string; profile?: null | string }

export function capabilityScoped(scope?: ProfileScope): { connectionId?: string; profile?: string } {
  if (scope && typeof scope === 'object') {
    const profile = (scope.profile ?? '').trim()
    const connectionId = (scope.connectionId ?? '').trim()

    return {
      ...(profile ? { profile } : {}),
      ...(connectionId ? { connectionId } : {})
    }
  }

  return { ...profileScoped(scope), ...connectionScoped() }
}

/** Stable cache-key for a capability scope: `profile` for the ambient/legacy
 *  path, `connectionId::profile` for ANY explicit pin — `local` included. An
 *  explicit "This device" pick and the ambient path are no longer guaranteed
 *  to hit the same backend (a remote registry PRIMARY makes the ambient path
 *  remote), so sharing the bare-profile cache row between them painted one
 *  machine's config under the other's scope (AGENTS.md scope-in-key rule). */
export function profileScopeKey(scope?: ProfileScope): string {
  if (scope && typeof scope === 'object') {
    const profile = (scope.profile ?? '').trim() || 'default'
    const connectionId = (scope.connectionId ?? '').trim()

    return connectionId ? `${connectionId}::${profile}` : profile
  }

  return (scope ?? '').trim() || 'default'
}

/** Registry connection id that connection-scoped WS calls should target
 *  (null → the local pool). Read-only twin of setApiRequestConnection. */
export function getApiRequestConnection(): null | string {
  return _apiConnectionId
}
