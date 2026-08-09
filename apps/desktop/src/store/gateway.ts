import { type ConnectionState, type GatewayEvent, resolveGatewayWsUrl } from '@hermes/shared'
import { atom } from 'nanostores'

import { HermesGateway } from '@/hermes'
import { reconnectBackoffDelayMs } from '@/lib/reconnect-backoff'
import { markNativeNotifyBaseline } from '@/store/notify-baseline'
import { setGatewayState } from '@/store/session'
import type { RpcEvent } from '@/types/hermes'

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
const LOCAL_TARGET_PREFIX = '__local__:'
const REMOTE_TARGET_PREFIX = '__remote__:'

type BackendMode = 'local' | 'remote'
type GatewayTargetMode = 'plain' | 'localOnly' | 'remoteOnly'

export interface GatewayProfileOptions {
  localOnly?: boolean
  remoteOnly?: boolean
}

export const gatewayTargetKey = (profile: string, options: GatewayProfileOptions = {}): string => {
  const key = normKey(profile)

  if (options.localOnly) {
    return `${LOCAL_TARGET_PREFIX}${key}`
  }

  if (options.remoteOnly) {
    return `${REMOTE_TARGET_PREFIX}${key}`
  }

  return key
}

function targetMode(options: GatewayProfileOptions = {}): GatewayTargetMode {
  if (options.localOnly) {
    return 'localOnly'
  }

  if (options.remoteOnly) {
    return 'remoteOnly'
  }

  return 'plain'
}

function targetOptions(mode: GatewayTargetMode): GatewayProfileOptions {
  if (mode === 'localOnly') {
    return { localOnly: true }
  }

  if (mode === 'remoteOnly') {
    return { remoteOnly: true }
  }

  return {}
}

function resolvedTargetKey(
  profile: string,
  options: GatewayProfileOptions,
  primaryMode: BackendMode,
  primaryProfile: string
): string {
  const key = normKey(profile)
  const target = gatewayTargetKey(key, options)

  if (key !== primaryProfile) {
    return target
  }

  if (options.localOnly && primaryMode === 'local') {
    return primaryProfile
  }

  if (options.remoteOnly && primaryMode === 'remote') {
    return primaryProfile
  }

  return target
}

// Read connection state through a call so TS control-flow analysis doesn't
// narrow the getter to a constant across guards (it genuinely changes).
const isOpen = (gateway: HermesGateway | null): boolean => gateway?.connectionState === 'open'

interface RegistryConfig {
  onEvent: (event: RpcEvent) => void
}

// ── Secondary (pool) backends ──────────────────────────────────────────────
interface Secondary {
  key: string
  profile: string
  localOnly: boolean
  remoteOnly: boolean
  gateway: HermesGateway
  offEvent: () => void
  offState: () => void
  reconnectTimer: ReturnType<typeof setTimeout> | null
  reconnectAttempt: number
  reconnecting: boolean
  // While true the entry auto-reconnects on drop; pruning flips it off so a
  // deliberate close doesn't trigger the backoff loop.
  wantOpen: boolean
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
  primaryBackendMode: BackendMode
  primaryProfile: string
  activeKey: string
  activeProfile: string
  activeTargetMode: GatewayTargetMode
  runtimeTargetKeys: Map<string, string>
  storedTargetKeys: Map<string, string>
  secondaries: Map<string, Secondary>
  $gateway: ReturnType<typeof atom<HermesGateway | null>>
}

const STATE_KEY = Symbol.for('hermes.desktop.gatewayRegistryState')

function createRegistryState(): GatewayRegistryState {
  return {
    config: null,
    primaryGateway: null,
    primaryBackendMode: 'local',
    primaryProfile: 'default',
    activeKey: 'default',
    activeProfile: 'default',
    activeTargetMode: 'plain',
    runtimeTargetKeys: new Map<string, string>(),
    storedTargetKeys: new Map<string, string>(),
    secondaries: new Map<string, Secondary>(),
    // The active gateway instance, exposed for inline message-stream
    // components (inline ClarifyTool, model overlays) that call gateway
    // methods without the instance threaded down through props.
    $gateway: atom<HermesGateway | null>(null)
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
    store[STATE_KEY] ??= createRegistryState()

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
}

/**
 * Feed a synthetic event through the exact same fan-out a real socket frame
 * takes (`config.onEvent` → the desktop's `handleGatewayEvent`). Used by
 * dev-only tooling to exercise the real event branches (e.g. the credit-notice
 * demo) without a backend that can produce the event on demand. No-op until a
 * registry is configured.
 */
export function emitLocalGatewayEvent(event: GatewayEvent): void {
  g.config?.onEvent(event as RpcEvent)
}

export function setPrimaryGateway(gateway: HermesGateway | null, profile = 'default'): void {
  g.primaryGateway = gateway
  g.primaryProfile = normKey(profile)
  syncActiveTarget()
}

export function setPrimaryBackendMode(mode: BackendMode): void {
  g.primaryBackendMode = mode
  syncActiveTarget()
}

export function activeGatewayTargetOptions(): GatewayProfileOptions {
  return targetOptions(g.activeTargetMode)
}

export function rememberGatewayRuntimeTarget(runtimeSessionId: string, key: string): void {
  if (!runtimeSessionId) {
    return
  }

  g.runtimeTargetKeys.set(runtimeSessionId, key)
}

export function bindStoredSessionGatewayTarget(runtimeSessionId: string, storedSessionId: null | string | undefined): void {
  const storedId = storedSessionId?.trim()

  if (!storedId) {
    return
  }

  g.storedTargetKeys.set(storedId, g.runtimeTargetKeys.get(runtimeSessionId) ?? g.activeKey)
}

export function sessionGatewayRetentionKey(profile: null | string | undefined, storedSessionId: null | string | undefined): string {
  const key = normKey(profile)

  if (key !== 'default') {
    return key
  }

  return storedSessionId?.trim() ? (g.storedTargetKeys.get(storedSessionId.trim()) ?? key) : key
}

export function primaryGatewayRetentionKey(profile: null | string | undefined): string {
  const key = normKey(profile)

  if (key !== g.primaryProfile) {
    return key
  }

  if (key !== 'default') {
    return key
  }

  return g.primaryBackendMode === 'remote' ? `${REMOTE_TARGET_PREFIX}${key}` : `${LOCAL_TARGET_PREFIX}${key}`
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
  // Any socket opening replays parked prompts; hold OS notifications so a
  // launch/reconnect doesn't alert about state that already existed.
  if (state === 'open') {
    markNativeNotifyBaseline()
  }

  if (normKey(profile) === g.activeKey) {
    setGatewayState(state)
  }
}

export function reportPrimaryGatewayState(state: ConnectionState): void {
  reportGatewayState(g.primaryProfile, state)
}

function setActive(key: string, profile: string, mode: GatewayTargetMode = 'plain'): void {
  g.activeKey = key
  g.activeProfile = normKey(profile)
  g.activeTargetMode = mode
  const gateway = activeGateway()
  g.$gateway.set(gateway)
  setGatewayState(gateway?.connectionState ?? 'closed')
}

function syncActiveTarget(): void {
  const key = resolvedTargetKey(g.activeProfile, targetOptions(g.activeTargetMode), g.primaryBackendMode, g.primaryProfile)

  if (key === g.activeKey) {
    return
  }

  setActive(key, g.activeProfile, g.activeTargetMode)
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

  const options = entry.localOnly ? { localOnly: true } : entry.remoteOnly ? { remoteOnly: true } : undefined
  const conn = await desktop.getConnection(entry.profile, options)
  const wsUrl = await resolveGatewayWsUrl(desktop, conn)
  await entry.gateway.connect(wsUrl)
  void desktop.touchBackend?.(entry.profile, options).catch(() => undefined)
}

function scheduleReconnect(entry: Secondary): void {
  if (entry.reconnecting || entry.reconnectTimer !== null || !entry.wantOpen) {
    return
  }

  // Full-jitter exponential backoff — same shape (and same reason: avoid a
  // reconnect storm against a restarting gateway) as the primary's.
  const delay = reconnectBackoffDelayMs(entry.reconnectAttempt)
  entry.reconnectAttempt += 1
  entry.reconnectTimer = setTimeout(() => {
    entry.reconnectTimer = null
    void reconnectSecondary(entry)
  }, delay)
}

async function reconnectSecondary(entry: Secondary): Promise<void> {
  if (entry.reconnecting || !entry.wantOpen || isOpen(entry.gateway)) {
    return
  }

  entry.reconnecting = true

  try {
    await openSecondary(entry)
    entry.reconnectAttempt = 0
  } catch {
    // Transport failure → fall through to the backoff below.
  } finally {
    entry.reconnecting = false

    if (entry.wantOpen && !isOpen(entry.gateway)) {
      scheduleReconnect(entry)
    }
  }
}

function createSecondary(key: string, profile: string, mode: GatewayTargetMode = 'plain'): Secondary {
  const gateway = new HermesGateway()

  const entry: Secondary = {
    key,
    profile,
    localOnly: mode === 'localOnly',
    remoteOnly: mode === 'remoteOnly',
    gateway,
    offEvent: () => {},
    offState: () => {},
    reconnectTimer: null,
    reconnectAttempt: 0,
    reconnecting: false,
    wantOpen: true
  }

  entry.offEvent = gateway.onEvent(event => g.config?.onEvent({ ...event, profile, targetKey: key }))
  entry.offState = gateway.onState(state => {
    reportGatewayState(key, state)

    if (state === 'open') {
      entry.reconnectAttempt = 0
      clearTimer(entry)
    } else if ((state === 'closed' || state === 'error') && entry.wantOpen) {
      scheduleReconnect(entry)
    }
  })

  g.secondaries.set(key, entry)

  return entry
}

// Open `profile`'s socket WITHOUT making it active — the hover-intent pre-warm
// (store/profile). Runs the same spawn + connect chain as a real switch, so by
// click time ensureGatewayForProfile finds an open socket and just activates
// it. No scheduleReconnect on failure: a hover is speculative, so a dead
// backend must not start a background retry loop — the real switch owns retry
// and error UX. An already-open (or primary) profile is a no-op.
export async function openGatewayForProfile(profile: string, options: GatewayProfileOptions = {}): Promise<void> {
  const mode = targetMode(options)
  const key = resolvedTargetKey(profile, options, g.primaryBackendMode, g.primaryProfile)

  if (key === g.primaryProfile) {
    return
  }

  const entry = g.secondaries.get(key) ?? createSecondary(key, profile, mode)
  entry.wantOpen = true

  if (!isOpen(entry.gateway)) {
    await openSecondary(entry)
  }
}

// Make `profile` the active gateway, lazily opening its socket if needed. The
// primary is a no-op fast path. Background sockets are never closed here.
export async function ensureGatewayForProfile(profile: string, options: GatewayProfileOptions = {}): Promise<void> {
  const mode = targetMode(options)
  const key = resolvedTargetKey(profile, options, g.primaryBackendMode, g.primaryProfile)

  if (key === g.primaryProfile) {
    setActive(key, profile, mode)

    return
  }

  let entry = g.secondaries.get(key)

  if (!entry) {
    entry = createSecondary(key, profile, mode)
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

  setActive(key, profile, mode)
}

// Reconnect the active gateway after a transient request failure. Primary
// reconnects are owned by use-gateway-boot, so we only drive secondaries here.
export async function ensureActiveGatewayOpen(): Promise<HermesGateway | null> {
  if (isActivePrimary()) {
    return g.primaryGateway
  }

  // The main-process pool can reap a backend while the renderer is asleep, and
  // profile switches/reloads can briefly leave the active key without a
  // secondary entry. Recreate the entry before declaring the active gateway
  // disconnected; otherwise callers surface a permanent generic error instead
  // of using the normal secondary reconnect path.
  let entry = g.secondaries.get(g.activeKey)

  if (!entry) {
    try {
      await ensureGatewayForProfile(g.activeProfile, targetOptions(g.activeTargetMode))
    } catch {
      return null
    }

    entry = g.secondaries.get(g.activeKey)
  }

  if (!entry) {
    return null
  }

  if (!isOpen(entry.gateway)) {
    await reconnectSecondary(entry)
  }

  return isOpen(entry.gateway) ? entry.gateway : null
}

// Wake signal (sleep/network/visibility): nudge every live secondary back open.
export function reconnectSecondaryGateways(): void {
  for (const entry of g.secondaries.values()) {
    if (!entry.wantOpen || isOpen(entry.gateway)) {
      continue
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
      const options = entry.localOnly ? { localOnly: true } : entry.remoteOnly ? { remoteOnly: true } : undefined
      void desktop?.touchBackend?.(entry.profile, options).catch(() => undefined)
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
}

// Close + evict secondaries whose profile is neither active nor in `keep`
// (profiles with a running / needs-input session). Bounds cost to live work.
export function pruneSecondaryGateways(keep: Set<string>): void {
  for (const [key, entry] of [...g.secondaries]) {
    if (key === g.activeKey || keep.has(key)) {
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

// Self-accept so editing this module (or a fan-out that lands here) is an
// in-place hot update instead of a full page reload — the live sockets in `g`
// survive the swap. Dev-only: production strips import.meta.hot.
if (import.meta.hot) {
  import.meta.hot.accept()
}
