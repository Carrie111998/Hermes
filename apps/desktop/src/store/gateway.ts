import {
  type ConnectionState,
  type GatewayEvent,
  isGatewayReauthRequired,
  resolveGatewayWsUrl
} from '@hermes/shared'
import { atom } from 'nanostores'

import { HermesGateway } from '@/hermes'
import { setConnection, setGatewayState } from '@/store/session'

// ── Multi-profile gateway routing ──────────────────────────────────────────
// Concurrent sessions across profiles need concurrent sockets: the renderer's
// event handler is already session-keyed, so the only thing stopping two
// profiles streaming at once was the single swapping socket. We keep that one
// socket as the PRIMARY (window) backend — owned by use-gateway-boot, with all
// its boot-progress / sleep-wake machinery — and add one persistent SECONDARY
// socket per *other* profile that has live work. Every socket feeds the same
// handleGatewayEvent, so background sessions keep painting. Single-profile users
// only ever have the primary, so their path is byte-for-byte unchanged.

const normKey = (profile: string | null | undefined): string => (profile ?? '').trim() || 'default'

// Read connection state through a call so TS control-flow analysis doesn't
// narrow the getter to a constant across guards (it genuinely changes).
const isOpen = (gateway: HermesGateway | null): boolean => gateway?.connectionState === 'open'

interface RegistryConfig {
  onEvent: (event: GatewayEvent) => void
  /** Per-profile connection-state sink (Layer 8). Boot wires this to the pet
   *  store's setProfileConnectionState; declared as a callback so gateway.ts
   *  never imports the pet store (which would be a cycle). Fires for primary and
   *  secondary state changes, reconnect starts/stops, reauth, and disposal. The
   *  active-only reportGatewayState mirroring into composer state is unchanged —
   *  background states must never flip the foreground composer. */
  onProfileState?: (
    profile: string,
    state: 'connecting' | 'offline' | 'open' | 'reauth-required',
    reason?: 'attempt-limit' | 'reauth'
  ) => void
}

// ── Secondary (pool) backends ──────────────────────────────────────────────
interface Secondary {
  profile: string
  gateway: HermesGateway
  offEvent: () => void
  offState: () => void
  reconnectTimer: ReturnType<typeof setTimeout> | null
  reconnectAttempt: number
  reconnecting: boolean
  // While true the entry auto-reconnects on drop; pruning flips it off so a
  // deliberate close doesn't trigger the backoff loop.
  wantOpen: boolean
  // Why the auto-reconnect loop stopped, or null while it may retry.
  // 'reauth' never auto-retries (only explicit post-sign-in retryProfileGateway
  // clears it); 'attempt-limit' (Layer 8) is reset by a wake/manual retry.
  retryStopped: 'attempt-limit' | 'reauth' | null
}

// ── HMR-stable module state ─────────────────────────────────────────────────
// All mutable singletons (live sockets, active-profile routing, the event
// registry) live in ONE container parked on globalThis, NOT in module-level
// `let`/`const` bindings. Reason: this module is imported widely without an HMR
// boundary that accepts it, so editing it (or anything that fans out to it)
// makes Vite issue a FULL PAGE RELOAD — which would kill every live socket and
// drop the agent session on an unrelated edit. Persisting the state on
// globalThis + self-accepting HMR (bottom of file) turns that full reload into
// an in-place hot update that preserves the sockets. Production strips
// import.meta.hot, and a fresh page realm starts with an empty container, so the
// runtime behavior is identical to plain module state.
interface GatewayRegistryState {
  config: RegistryConfig | null
  primaryGateway: HermesGateway | null
  primaryProfile: string
  activeKey: string
  secondaries: Map<string, Secondary>
  $gateway: ReturnType<typeof atom<HermesGateway | null>>
  // ── Lease + reconnect-ownership state (all HMR-migrated in gatewayState) ──
  // profile -> refcount. A profile with a positive refcount is spared by the
  // idle prune even when it has no live work (roster streaming / a background
  // submit holds it open).
  leasedProfiles: Map<string, number>
  // profile -> in-flight open promise, so concurrent lease/request opens for the
  // same profile spawn a single socket.
  profileOpens: Map<string, Promise<void>>
  // Leases acquired before the registry is configured (boot); drained by
  // configureGatewayRegistry.
  pendingLeases: Set<string>
  leasePruneTimer: ReturnType<typeof setTimeout> | null
  // The most recent working/attention keep-set from use-gateway-boot, folded
  // into the lease prune so live work is never dropped.
  lastKnownKeepSet: Set<string>
  // profile -> stored OAuth reauth error. The active hook drains its own entry
  // (takeGatewayReauthError); Layer 8 reads background entries for offline state.
  reauthErrors: Map<string, unknown>
  // In-flight primary reconnect, shared by concurrent callers (dedup).
  primaryReconnect: Promise<HermesGateway | null> | null
}

const STATE_KEY = Symbol.for('hermes.desktop.gatewayRegistryState')

function createRegistryState(): GatewayRegistryState {
  return {
    config: null,
    primaryGateway: null,
    primaryProfile: 'default',
    activeKey: 'default',
    secondaries: new Map<string, Secondary>(),
    // The active gateway instance, exposed for inline message-stream
    // components (inline ClarifyTool, model overlays) that call gateway
    // methods without the instance threaded down through props.
    $gateway: atom<HermesGateway | null>(null),
    leasedProfiles: new Map<string, number>(),
    profileOpens: new Map<string, Promise<void>>(),
    pendingLeases: new Set<string>(),
    leasePruneTimer: null,
    lastKnownKeepSet: new Set<string>(),
    reauthErrors: new Map<string, unknown>(),
    primaryReconnect: null
  }
}

// Dev only: park the singletons on globalThis so an HMR re-eval of this module
// (self-accepted at the bottom) hands back the SAME live sockets/atoms instead
// of resetting them — that's what keeps the agent session alive across UI edits.
// `import.meta.hot` is undefined in production, so Vite dead-code-eliminates the
// entire globalThis branch and prod uses a plain module-local singleton — no
// globalThis, no Symbol.for. Both realms load the module once, so the container's
// shape and lifetime are identical either way.
function gatewayState(): GatewayRegistryState {
  if (import.meta.hot) {
    const store = globalThis as unknown as { [STATE_KEY]?: GatewayRegistryState }
    const existing = store[STATE_KEY]

    if (existing) {
      // Field-level migration: an HMR container created by a prior version of
      // this module lacks the lease/reconnect-ownership fields (and old live
      // Secondary entries lack retryStopped). Add them in place so the hot
      // update keeps the live sockets AND gains the new state. `??=` never
      // clobbers a field a newer container already initialized.
      existing.leasedProfiles ??= new Map()
      existing.profileOpens ??= new Map()
      existing.pendingLeases ??= new Set()
      existing.leasePruneTimer ??= null
      existing.lastKnownKeepSet ??= new Set()
      existing.reauthErrors ??= new Map()
      existing.primaryReconnect ??= null

      for (const entry of existing.secondaries.values()) {
        entry.retryStopped ??= null
      }

      return existing
    }

    store[STATE_KEY] = createRegistryState()

    return store[STATE_KEY]
  }

  return createRegistryState()
}

const g = gatewayState()

// Re-exported as a stable binding: the atom instance lives in `g`, so every hot
// reload of this module hands back the SAME atom subscribers are already wired
// to. (A fresh `atom()` per reload would orphan existing subscriptions.)
export const $gateway = g.$gateway

export function configureGatewayRegistry(cfg: RegistryConfig): void {
  g.config = cfg

  // Drain any leases acquired before the registry existed (boot ordering): the
  // roster controller can lease a profile before the gateway effect wires the
  // event handler. Open them now that a registry is present.
  if (g.pendingLeases.size > 0) {
    const pending = [...g.pendingLeases]
    g.pendingLeases.clear()

    for (const key of pending) {
      void ensureSecondaryOpen(key).catch(() => undefined)
    }
  }
}

/**
 * Feed a synthetic event through the exact same fan-out a real socket frame
 * takes (`config.onEvent` → the desktop's `handleGatewayEvent`). Used by
 * dev-only tooling to exercise the real event branches (e.g. the credit-notice
 * demo) without a backend that can produce the event on demand. No-op until a
 * registry is configured.
 */
export function emitLocalGatewayEvent(event: GatewayEvent): void {
  g.config?.onEvent(event)
}

export function setPrimaryGateway(gateway: HermesGateway | null, profile = 'default'): void {
  g.primaryGateway = gateway
  g.primaryProfile = normKey(profile)
}

export function isActivePrimary(): boolean {
  return g.activeKey === g.primaryProfile
}

export function activeGateway(): HermesGateway | null {
  if (g.activeKey === g.primaryProfile) {
    return g.primaryGateway
  }

  return g.secondaries.get(g.activeKey)?.gateway ?? g.primaryGateway
}

// Mirror a backend's connection state into the global composer state, but only
// when that backend is the one the user is currently looking at. Lets the
// composer reflect the active profile's socket without a background reconnect
// flipping the foreground enabled/disabled state.
function reportGatewayState(profile: string, state: ConnectionState): void {
  if (normKey(profile) === g.activeKey) {
    setGatewayState(state)
  }
}

export function reportPrimaryGatewayState(state: ConnectionState): void {
  reportGatewayState(g.primaryProfile, state)
}

/** Pooled/background gateways retry a dead backend on a bounded backoff
 *  (1s, 2s, 4s, 8s, 15s, 15s); a sixth failure stops the loop as 'attempt-limit'. */
const MAX_BG_RETRIES = 6

// Map a transport ConnectionState onto the pet store's connection vocabulary.
function toProfileConnection(state: ConnectionState): 'connecting' | 'offline' | 'open' {
  if (state === 'open') {
    return 'open'
  }

  return state === 'connecting' ? 'connecting' : 'offline'
}

/** Report a profile's socket state to the per-profile sink (Layer 8). Every
 *  profile (primary + secondary) flows through here; the callback is boot-wired
 *  to the pet store, so a dead pinned profile shows an offline pet. */
function reportProfileState(
  profile: string,
  state: 'connecting' | 'offline' | 'open' | 'reauth-required',
  reason?: 'attempt-limit' | 'reauth'
): void {
  g.config?.onProfileState?.(normKey(profile), state, reason)
}

function setActive(profile: string): void {
  g.activeKey = normKey(profile)
  const gateway = activeGateway()
  g.$gateway.set(gateway)
  setGatewayState(gateway?.connectionState ?? 'closed')
}

function clearTimer(entry: Secondary): void {
  if (entry.reconnectTimer !== null) {
    clearTimeout(entry.reconnectTimer)
    entry.reconnectTimer = null
  }
}

async function openSecondary(entry: Secondary): Promise<void> {
  const desktop = window.hermesDesktop

  if (!desktop) {
    return
  }

  const conn = await desktop.getConnection(entry.profile)
  const wsUrl = await resolveGatewayWsUrl(desktop, conn)
  await entry.gateway.connect(wsUrl)
  void desktop.touchBackend?.(entry.profile).catch(() => undefined)
}

function scheduleReconnect(entry: Secondary): void {
  // A reauth stop never auto-retries: the ticket can never succeed until the
  // user signs in again, so looping the backoff just spins silently. Only an
  // explicit post-sign-in retryProfileGateway clears it.
  if (entry.reconnecting || entry.reconnectTimer !== null || !entry.wantOpen || entry.retryStopped === 'reauth') {
    return
  }

  // Bounded backoff (Layer 8): after six failed retries stop the loop, publish
  // offline, and cancel the timer. A wake signal, manual retry, or
  // disable/re-enable resets the stop (retryProfileGateway).
  if (entry.reconnectAttempt >= MAX_BG_RETRIES) {
    entry.retryStopped = 'attempt-limit'
    clearTimer(entry)
    reportProfileState(entry.profile, 'offline', 'attempt-limit')

    return
  }

  // 1s, 2s, 4s … capped at 15s — same backoff shape as the primary.
  const delay = Math.min(15_000, 1_000 * 2 ** Math.min(entry.reconnectAttempt, 4))
  entry.reconnectAttempt += 1
  reportProfileState(entry.profile, 'connecting')
  entry.reconnectTimer = setTimeout(() => {
    entry.reconnectTimer = null
    void reconnectSecondary(entry)
  }, delay)
}

async function reconnectSecondary(entry: Secondary): Promise<void> {
  if (entry.reconnecting || !entry.wantOpen || isOpen(entry.gateway) || entry.retryStopped === 'reauth') {
    return
  }

  entry.reconnecting = true

  try {
    await openSecondary(entry)
    entry.reconnectAttempt = 0
    entry.retryStopped = null
    g.reauthErrors.delete(entry.profile)
  } catch (error) {
    // OAuth reauth: store the error and stop the loop (never auto-retries).
    // Transport failure → fall through to the bounded backoff below.
    if (isGatewayReauthRequired(error)) {
      g.reauthErrors.set(entry.profile, error)
      entry.retryStopped = 'reauth'
      clearTimer(entry)
      reportProfileState(entry.profile, 'reauth-required', 'reauth')
    }
  } finally {
    entry.reconnecting = false

    if (entry.wantOpen && !isOpen(entry.gateway) && entry.retryStopped !== 'reauth') {
      scheduleReconnect(entry)
    }
  }
}

function createSecondary(profile: string): Secondary {
  const gateway = new HermesGateway()

  const entry: Secondary = {
    profile,
    gateway,
    offEvent: () => {},
    offState: () => {},
    reconnectTimer: null,
    reconnectAttempt: 0,
    reconnecting: false,
    wantOpen: true,
    retryStopped: null
  }

  entry.offEvent = gateway.onEvent(event => g.config?.onEvent({ ...event, profile }))
  entry.offState = gateway.onState(state => {
    reportGatewayState(profile, state)
    reportProfileState(profile, toProfileConnection(state))

    if (state === 'open') {
      entry.reconnectAttempt = 0
      clearTimer(entry)
    } else if ((state === 'closed' || state === 'error') && entry.wantOpen) {
      scheduleReconnect(entry)
    }
  })

  g.secondaries.set(profile, entry)

  return entry
}

// Open `profile`'s socket WITHOUT making it active — the hover-intent pre-warm
// (store/profile). Runs the same spawn + connect chain as a real switch, so by
// click time ensureGatewayForProfile finds an open socket and just activates
// it. No scheduleReconnect on failure: a hover is speculative, so a dead
// backend must not start a background retry loop — the real switch owns retry
// and error UX. An already-open (or primary) profile is a no-op.
export async function openGatewayForProfile(profile: string): Promise<void> {
  const key = normKey(profile)

  if (key === g.primaryProfile) {
    return
  }

  const entry = g.secondaries.get(key) ?? createSecondary(key)
  entry.wantOpen = true

  if (!isOpen(entry.gateway)) {
    await openSecondary(entry)
  }
}

// Make `profile` the active gateway, lazily opening its socket if needed. The
// primary is a no-op fast path. Background sockets are never closed here.
export async function ensureGatewayForProfile(profile: string): Promise<void> {
  const key = normKey(profile)

  if (key === g.primaryProfile) {
    setActive(key)

    return
  }

  let entry = g.secondaries.get(key)

  if (!entry) {
    entry = createSecondary(key)
  }

  entry.wantOpen = true

  if (!isOpen(entry.gateway)) {
    clearTimer(entry)
    entry.reconnectAttempt = 0

    try {
      await openSecondary(entry)
    } catch {
      scheduleReconnect(entry)
    }
  }

  setActive(key)
}

// Reconnect the active gateway after a transient request failure. Primary
// reconnects are owned by use-gateway-boot, so we only drive secondaries here.
export async function ensureActiveGatewayOpen(): Promise<HermesGateway | null> {
  if (g.activeKey === g.primaryProfile) {
    return g.primaryGateway
  }

  const entry = g.secondaries.get(g.activeKey)

  if (!entry) {
    return null
  }

  if (!isOpen(entry.gateway)) {
    await reconnectSecondary(entry)
  }

  return isOpen(entry.gateway) ? entry.gateway : null
}

// Wake signal (sleep/network/visibility): nudge every live secondary back open.
// Resets an attempt-limit stop (a fresh wake gets a full retry budget) but never
// clears a reauth stop — reconnectSecondary refuses to auto-retry reauth.
export function reconnectSecondaryGateways(): void {
  for (const entry of g.secondaries.values()) {
    if (!entry.wantOpen || isOpen(entry.gateway)) {
      continue
    }

    if (entry.retryStopped === 'attempt-limit') {
      entry.retryStopped = null
    }

    entry.reconnectAttempt = 0
    clearTimer(entry)
    void reconnectSecondary(entry)
  }
}

// Keep the idle reaper from killing a backend we still need: ping every live
// secondary. The active one is pinged separately (touchActiveGatewayBackend).
export function touchSecondaryGateways(): void {
  const desktop = window.hermesDesktop

  for (const entry of g.secondaries.values()) {
    if (entry.wantOpen) {
      void desktop?.touchBackend?.(entry.profile).catch(() => undefined)
    }
  }
}

// Tear a secondary down: stop its reconnect loop, detach listeners, close the
// socket. Caller handles removal from the map.
function disposeSecondary(entry: Secondary): void {
  entry.wantOpen = false
  clearTimer(entry)
  entry.offEvent()
  entry.offState()
  entry.gateway.close()
  reportProfileState(entry.profile, 'offline')
}

// Close + evict secondaries whose profile is neither active nor in `keep`
// (profiles with a running / needs-input session). Bounds cost to live work.
// Leased profiles (positive refcount) are always spared: a lease is an explicit
// "keep this socket alive" independent of live work (roster streaming, a
// background submit). The active profile is spared by the caller's keep-set
// union AND here, so a lease release can never drop the foreground socket.
export function pruneSecondaryGateways(keep: Set<string>): void {
  const effective = new Set(keep)

  for (const [key, count] of g.leasedProfiles) {
    if (count > 0) {
      effective.add(key)
    }
  }

  for (const [key, entry] of [...g.secondaries]) {
    if (key === g.activeKey || effective.has(key)) {
      continue
    }

    disposeSecondary(entry)
    g.secondaries.delete(key)
  }
}

export function closeSecondaryGateways(): void {
  for (const entry of g.secondaries.values()) {
    disposeSecondary(entry)
  }

  g.secondaries.clear()
}

// ── Profile-specific RPC + reconnect ownership + leases ────────────────────
// One routing rule for every profile-owned gateway operation: resolve the
// connection through the registry (primary socket or a per-profile secondary),
// NEVER through whichever gateway happens to be active. A lease keeps a socket
// alive; it is not permission to route.

const isTransientConnectionError = (error: unknown): boolean => {
  const message = error instanceof Error ? error.message : String(error)

  return /not connected|connection closed/i.test(message)
}

/** The registry's own primary profile key — read at emit time, not snapshotted.
 *  Primary events are tagged with this (Layer 4) so a gateway rebuild while a
 *  secondary is active never mis-tags them with the secondary's profile. */
export function primaryProfileKey(): string {
  return g.primaryProfile
}

/** Non-destructive read of a profile's stored reauth error (Layer 8 status). */
export function gatewayReauthError(profile: string): unknown | null {
  return g.reauthErrors.get(normKey(profile)) ?? null
}

/** Drain a profile's stored reauth error (the active hook surfaces it once). */
export function takeGatewayReauthError(profile: string): unknown | null {
  const key = normKey(profile)
  const error = g.reauthErrors.get(key) ?? null
  g.reauthErrors.delete(key)

  return error
}

/**
 * Recover a dropped PRIMARY gateway. Extracted from use-gateway-request's
 * ensureGatewayOpen so a NON-active primary can be recovered (e.g. a leased
 * background profile that happens to be the window's primary backend) without
 * publishing its connection as the foreground one.
 *
 * - profile is a parameter, not `$activeGatewayProfile`
 * - `setConnection` fires ONLY when this profile is the active one
 * - concurrent callers share `g.primaryReconnect` (dedup)
 * - reauth errors are stored per profile; cleared on success
 */
export async function reconnectPrimaryGateway(profile: string): Promise<HermesGateway | null> {
  const key = normKey(profile)
  const gateway = g.primaryGateway

  if (!gateway) {
    return null
  }

  if (isOpen(gateway)) {
    return gateway
  }

  if (g.primaryReconnect) {
    return g.primaryReconnect
  }

  g.primaryReconnect = (async () => {
    const desktop = window.hermesDesktop

    if (!desktop) {
      return null
    }

    try {
      const conn = await desktop.getConnection(key)

      // A background reconnect must not publish its connection as the
      // foreground one — only the active profile mirrors into composer state.
      if (key === g.activeKey) {
        setConnection(conn)
      }

      // Re-mint the WS URL: OAuth tickets are single-use, so the cached
      // conn.wsUrl ticket is dead on every reconnect after boot.
      const wsUrl = await resolveGatewayWsUrl(desktop, conn)
      await gateway.connect(wsUrl)
      g.reauthErrors.delete(key)

      return gateway
    } catch (error) {
      if (isGatewayReauthRequired(error)) {
        g.reauthErrors.set(key, error)
      }

      if (key === g.activeKey) {
        setConnection(null)
      }

      return null
    } finally {
      g.primaryReconnect = null
    }
  })()

  return g.primaryReconnect
}

/**
 * Open (or join an in-flight open of) a secondary profile's socket, deduped via
 * `g.profileOpens` so concurrent lease/request opens spawn a single socket.
 * Classifies reauth (store + stop) vs transport failure (bounded backoff).
 * Resolves once the socket is open; rejects if it could not be opened.
 */
async function ensureSecondaryOpen(key: string): Promise<HermesGateway> {
  const entry = g.secondaries.get(key) ?? createSecondary(key)
  entry.wantOpen = true

  if (isOpen(entry.gateway)) {
    return entry.gateway
  }

  let pending = g.profileOpens.get(key)

  if (!pending) {
    pending = (async () => {
      try {
        await openSecondary(entry)
        entry.reconnectAttempt = 0
        entry.retryStopped = null
        g.reauthErrors.delete(key)
      } catch (error) {
        if (isGatewayReauthRequired(error)) {
          g.reauthErrors.set(key, error)
          entry.retryStopped = 'reauth'
          clearTimer(entry)
        } else {
          // Retryable transport failure enters the bounded backoff immediately.
          scheduleReconnect(entry)
        }

        throw error
      } finally {
        g.profileOpens.delete(key)
      }
    })()

    g.profileOpens.set(key, pending)
  }

  await pending

  if (!isOpen(entry.gateway)) {
    throw new Error(`gateway for profile "${key}" is not connected`)
  }

  return entry.gateway
}

/**
 * Route a gateway RPC to a specific profile WITHOUT changing the active
 * gateway. Primary → `g.primaryGateway` (recover via reconnectPrimaryGateway);
 * secondary → its own socket (open/recover via the bounded secondary path).
 * NEVER falls back to the active gateway — a failure surfaces as a clear error.
 */
export async function requestGatewayForProfile<T>(
  profile: string,
  method: string,
  params: Record<string, unknown> = {},
  timeoutMs?: number,
  signal?: AbortSignal
): Promise<T> {
  const key = normKey(profile)

  if (key === g.primaryProfile) {
    const gateway = g.primaryGateway

    if (!gateway) {
      throw new Error('Hermes gateway unavailable')
    }

    try {
      return await gateway.request<T>(method, params, timeoutMs, signal)
    } catch (error) {
      if (!isTransientConnectionError(error)) {
        throw error
      }

      const recovered = await reconnectPrimaryGateway(key)

      if (!recovered) {
        const reauthError = takeGatewayReauthError(key)

        if (reauthError) {
          throw reauthError
        }

        throw error
      }

      return recovered.request<T>(method, params, timeoutMs, signal)
    }
  }

  const gateway = await ensureSecondaryOpen(key)

  try {
    return await gateway.request<T>(method, params, timeoutMs, signal)
  } catch (error) {
    if (!isTransientConnectionError(error)) {
      throw error
    }

    const entry = g.secondaries.get(key)

    if (entry) {
      await reconnectSecondary(entry)
    }

    const recovered = g.secondaries.get(key)?.gateway

    if (!recovered || !isOpen(recovered)) {
      const reauthError = takeGatewayReauthError(key)

      if (reauthError) {
        throw reauthError
      }

      throw error
    }

    return recovered.request<T>(method, params, timeoutMs, signal)
  }
}

/**
 * Manual/wake retry of a profile's gateway. Resets an attempt-limit stop and
 * clears a stored reauth error (callers invoke this AFTER successful sign-in),
 * then nudges a reconnect. Primary and secondary both handled.
 */
export function retryProfileGateway(profile: string): void {
  const key = normKey(profile)

  if (key === g.primaryProfile) {
    g.reauthErrors.delete(key)
    void reconnectPrimaryGateway(key)

    return
  }

  const entry = g.secondaries.get(key)

  if (!entry) {
    return
  }

  entry.retryStopped = null
  entry.reconnectAttempt = 0
  g.reauthErrors.delete(key)
  clearTimer(entry)
  void reconnectSecondary(entry)
}

// ── Leases ─────────────────────────────────────────────────────────────────
// A lease keeps a profile's socket alive independent of live work. Refcounted:
// the roster holds a persistent lease per enabled pinned profile; a background
// submit takes a temporary one via withProfileGatewayLease. Release at zero
// schedules a debounced prune (the keep-set union still spares live work).

/** Debounced 50ms prune after a lease release; coalesces a release burst. */
function scheduleLeasePrune(): void {
  if (g.leasePruneTimer !== null) {
    return
  }

  g.leasePruneTimer = setTimeout(() => {
    g.leasePruneTimer = null
    pruneSecondaryGateways(new Set(g.lastKnownKeepSet))
  }, 50)
}

/**
 * Acquire (or bump) a lease on `profile`'s socket and open it. Pre-boot leases
 * are queued and drained by configureGatewayRegistry. Stays synchronous;
 * callers needing readiness await requestGatewayForProfile.
 */
export function leaseProfileGateway(profile: string): void {
  const key = normKey(profile)
  g.leasedProfiles.set(key, (g.leasedProfiles.get(key) ?? 0) + 1)

  if (!g.config) {
    g.pendingLeases.add(key)

    return
  }

  void ensureSecondaryOpen(key).catch(() => undefined)
}

/**
 * Release one reference on `profile`'s lease. No-op when absent or already zero
 * (refcounts never go negative — React StrictMode cleanup and repeated catalog
 * refreshes are safe). At zero, schedules a prune unless the profile is active.
 */
export function releaseProfileGateway(profile: string): void {
  const key = normKey(profile)
  const count = g.leasedProfiles.get(key)

  if (!count) {
    return
  }

  if (count > 1) {
    g.leasedProfiles.set(key, count - 1)

    return
  }

  g.leasedProfiles.delete(key)

  if (key !== g.activeKey) {
    scheduleLeasePrune()
  }
}

/** The only temporary-lease pattern: acquire, run, release in `finally`. */
export async function withProfileGatewayLease<T>(profile: string, run: () => Promise<T>): Promise<T> {
  leaseProfileGateway(profile)

  try {
    return await run()
  } finally {
    releaseProfileGateway(profile)
  }
}

/** use-gateway-boot publishes its working/attention keep-set here on every
 *  recompute, before pruning, so the lease prune never drops live work. */
export function updateGatewayKeepSet(keep: ReadonlySet<string>): void {
  g.lastKnownKeepSet = new Set(keep)
}

/**
 * Test-only: reset the registry's mutable routing/lease state so cases don't
 * leak active-key, secondary sockets, or leases into one another (the container
 * is module-global and otherwise lives for the whole file). Closes live
 * secondaries and clears timers; the stable `$gateway` atom identity is kept.
 * Not used by production code.
 */
export function __resetGatewayRegistryForTests(): void {
  for (const entry of g.secondaries.values()) {
    disposeSecondary(entry)
  }

  g.secondaries.clear()
  g.leasedProfiles.clear()
  g.profileOpens.clear()
  g.pendingLeases.clear()
  g.lastKnownKeepSet.clear()
  g.reauthErrors.clear()

  if (g.leasePruneTimer !== null) {
    clearTimeout(g.leasePruneTimer)
    g.leasePruneTimer = null
  }

  g.primaryReconnect = null
  g.primaryGateway = null
  g.primaryProfile = 'default'
  g.activeKey = 'default'
  g.config = null
}

// Self-accept so editing this module (or a fan-out that lands here) is an
// in-place hot update instead of a full page reload — the live sockets in `g`
// survive the swap. Dev-only: production strips import.meta.hot.
if (import.meta.hot) {
  import.meta.hot.accept()
}
