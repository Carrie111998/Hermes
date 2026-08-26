/**
 * gateway-supervisor.ts — Magnum #94724 Phase 2
 *
 * Centralized lifecycle authority in Electron main (process-wide).
 *
 * Responsibilities:
 *  - lifecycle / activation / route publication (§4, §6, §7)
 *  - reconnect single-flight deduplicated by (connectionId, generation) (§5)
 *  - generation invalidation: stale results never publish (§3.1 §20)
 *  - handshake gate: WebSocket.OPEN alone never publishes (§7) — every
 *    ActivationGate condition is checked against post-dial transport facts
 *    before a lease is minted
 *  - retention leases (§8) — thin wrapper, real counting lives in retention-lease.ts
 *
 * Does not own transport itself; it owns AUTHORITY to decide when transport
 * may publish. Backends/sockets remain in their existing owners; this module
 * is the single gate they must pass through.
 */

import type { RouteKey } from './connection-route-identity'

// ── TransportHandle — what activateTransport hands back (§7)
//
// `descriptor` is the payload the caller actually needs (the dialed backend
// connection). It travels on the lease so a result can only ever be read back
// through the route it was dialed for — carrying it out-of-band (a module
// global, a shared slot) is exactly the cross-route aliasing this file exists
// to prevent.
export type TransportHandle<D = unknown> = Readonly<{
  gatewayEpoch: string
  socketInstanceId: string
  descriptor: D
  gatewayReady: boolean
  targetProfileMatches: boolean
}>

// ── RouteLease — ephemeral authorization to execute NOW on a route (§3.3)
export type RouteLease<D = unknown> = Readonly<{
  route: RouteKey
  activationEpoch: number
  socketInstanceId: string
  gatewayEpoch: string
  descriptor: D
}>

// ── ActivationReceipt — typed publication outcome (§7)
export type ActivationReceipt<D = unknown> =
  | Readonly<{ status: 'activated'; route: RouteKey; lease: RouteLease<D> }>
  | Readonly<{ status: 'already-active'; route: RouteKey; lease: RouteLease<D> }>
  | Readonly<{
      status: 'offline' | 'superseded' | 'revoked' | 'removed' | 'cancelled' | 'ambiguous'
      route: RouteKey
      reason?: string
    }>

// ── Explicit lifecycle state machine (§6) — no boolean explosion
export type GatewaySupervisorState =
  | 'Dormant'
  | 'Resolving'
  | 'Provisioning'
  | 'Dialing'
  | 'Handshaking'
  | 'Live'
  | 'Degraded'
  | 'Backoff'
  | 'Draining'

const VALID_TRANSITIONS: Record<GatewaySupervisorState, ReadonlySet<GatewaySupervisorState>> = {
  Dormant: new Set<GatewaySupervisorState>(['Resolving']),
  Resolving: new Set<GatewaySupervisorState>(['Provisioning', 'Dormant']),
  Provisioning: new Set<GatewaySupervisorState>(['Dialing', 'Dormant']),
  Dialing: new Set<GatewaySupervisorState>(['Handshaking', 'Backoff', 'Dormant']),
  Handshaking: new Set<GatewaySupervisorState>(['Live', 'Backoff', 'Dormant']),
  Live: new Set<GatewaySupervisorState>(['Degraded', 'Draining', 'Backoff']),
  Degraded: new Set<GatewaySupervisorState>(['Backoff', 'Draining', 'Live']),
  Backoff: new Set<GatewaySupervisorState>(['Dialing', 'Dormant']),
  Draining: new Set<GatewaySupervisorState>(['Dormant']),
}

export function canTransition(from: GatewaySupervisorState, to: GatewaySupervisorState): boolean {
  return Boolean(VALID_TRANSITIONS[from]?.has(to))
}

// ── Activation gate — all of these must hold before publication (§7)
export interface ActivationGate {
  transportOpen: boolean
  gatewayReady: boolean
  generationCurrent: boolean
  targetProfileMatches: boolean
  gatewayEpochKnown: boolean
}

export function isActivationGateOpen(gate: ActivationGate): boolean {
  return (
    gate.transportOpen &&
    gate.gatewayReady &&
    gate.generationCurrent &&
    gate.targetProfileMatches &&
    gate.gatewayEpochKnown
  )
}

export function supervisorKey(route: RouteKey): string {
  return `${String(route.connectionId)}::${route.generation}::${String(route.desktopProfile)}`
}

// ── Supervisor
export type ActivateFn<D = unknown> = (route: RouteKey) => Promise<TransportHandle<D>>
export type IsCurrentFn = (route: RouteKey) => boolean

export class GatewaySupervisor<D = unknown> {
  readonly #flights = new Map<string, Promise<ActivationReceipt<D>>>()
  readonly #states = new Map<string, GatewaySupervisorState>()
  readonly #activationEpochByKey = new Map<string, number>()
  readonly #leases = new Map<string, RouteLease<D>>()

  constructor(
    private readonly deps: {
      activateTransport: ActivateFn<D>
      isRouteCurrent: IsCurrentFn
      onTransition?: (key: string, from: GatewaySupervisorState, to: GatewaySupervisorState) => void
    }
  ) {}

  stateFor(route: RouteKey): GatewaySupervisorState {
    return this.#states.get(supervisorKey(route)) ?? 'Dormant'
  }

  leaseFor(route: RouteKey): RouteLease<D> | null {
    return this.#leases.get(supervisorKey(route)) ?? null
  }

  transition(route: RouteKey, to: GatewaySupervisorState): boolean {
    const key = supervisorKey(route)
    const from = this.#states.get(key) ?? 'Dormant'

    if (!canTransition(from, to)) {
      return false
    }
    this.#states.set(key, to)
    this.deps.onTransition?.(key, from, to)

    return true
  }

  /**
   * Activate a route. Single-flight per (connectionId, generation, profile):
   * concurrent callers for the same logical route coalesce onto one dial.
   * Stale callers (generation bumped mid-flight) resolve as `superseded` and
   * never publish.
   *
   * `dial` lets a caller supply the transport for its own call site — the
   * primary handler dials the primary the way it always has, the registry
   * handler dials the registry entry. The supervisor owns coalescing,
   * currency and gating; it does not second-guess which backend a caller
   * meant. Coalesced callers share the first caller's dial, which is the
   * same contract BackendDialClaims already has.
   */
  activate(route: RouteKey, dial?: ActivateFn<D>): Promise<ActivationReceipt<D>> {
    const key = supervisorKey(route)
    const existing = this.#flights.get(key)

    if (existing) {
      return existing
    }

    // If route is already stale at call time, fail closed immediately.
    if (!this.deps.isRouteCurrent(route)) {
      return Promise.resolve({ status: 'superseded', route, reason: 'stale-at-call' })
    }

    const pending = (async (): Promise<ActivationReceipt<D>> => {
      // Advance through Resolving → Dialing → Handshaking, respecting the machine.
      this.transition(route, 'Resolving')
      this.transition(route, 'Provisioning')
      this.transition(route, 'Dialing')

      let transport: TransportHandle<D>

      try {
        transport = await (dial ?? this.deps.activateTransport)(route)
      } catch (error) {
        this.transition(route, 'Backoff')
        const message = error instanceof Error ? error.message : String(error)

        // Generation may have moved while the dial was in flight — stale beats offline.
        if (!this.deps.isRouteCurrent(route)) {
          return { status: 'superseded', route, reason: message }
        }

        return { status: 'offline', route, reason: message }
      }

      // Re-check currency after the async gap: generation bump during dial invalidates.
      if (!this.deps.isRouteCurrent(route)) {
        this.transition(route, 'Dormant')

        return { status: 'superseded', route, reason: 'generation-bumped-during-dial' }
      }

      this.transition(route, 'Handshaking')

      // §7: transport being open is not enough to publish. Every gate must hold
      // on the post-dial facts, or the route stays unpublished.
      const gate: ActivationGate = {
        transportOpen: Boolean(transport.socketInstanceId),
        gatewayReady: transport.gatewayReady,
        generationCurrent: this.deps.isRouteCurrent(route),
        targetProfileMatches: transport.targetProfileMatches,
        gatewayEpochKnown: Boolean(transport.gatewayEpoch),
      }

      if (!isActivationGateOpen(gate)) {
        this.transition(route, 'Backoff')
        const unmet = (Object.keys(gate) as (keyof ActivationGate)[]).filter(k => !gate[k])

        return { status: 'offline', route, reason: `activation gate closed: ${unmet.join(', ')}` }
      }

      const epoch = (this.#activationEpochByKey.get(key) ?? 0) + 1
      this.#activationEpochByKey.set(key, epoch)

      const lease: RouteLease<D> = {
        route,
        activationEpoch: epoch,
        socketInstanceId: transport.socketInstanceId,
        gatewayEpoch: transport.gatewayEpoch,
        descriptor: transport.descriptor,
      }

      this.#leases.set(key, lease)
      this.transition(route, 'Live')

      return { status: 'activated', route, lease }
    })()

    const release = () => {
      if (this.#flights.get(key) === pending) {
        this.#flights.delete(key)
      }
    }

    this.#flights.set(key, pending)
    void pending.then(release, release)

    return pending
  }

  /** Alias: reconnect is an activation under the same single-flight. */
  reconnect(route: RouteKey, dial?: ActivateFn<D>): Promise<ActivationReceipt<D>> {
    return this.activate(route, dial)
  }

  dispose(route: RouteKey): void {
    const key = supervisorKey(route)
    this.#flights.delete(key)
    this.#leases.delete(key)
    // Draining → Dormant is the only legal dispose path; force it if needed.
    const cur = this.#states.get(key)

    if (cur === 'Live' || cur === 'Degraded') {
      this.transition(route, 'Draining')
    }
    this.#states.delete(key)
    // Do not clear activationEpoch — a future re-activation must get a fresh epoch.
  }

  inFlight(key: string): boolean {
    return this.#flights.has(key)
  }

  // Test/diagnostic: current flight keys
  flightKeys(): string[] {
    return [...this.#flights.keys()]
  }
}
