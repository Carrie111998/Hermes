import { atom, computed } from 'nanostores'

import type { DesktopConnectionsRegistry } from '@/global'
import { persistStringRecord, storedStringRecord } from '@/lib/storage'
import { wipeSessionListsForGatewaySwitch } from '@/store/gateway-switch'
import {
  $activeGatewayProfile,
  $newChatProfile,
  $showAllProfiles,
  ensureGatewayAgent,
  normalizeProfileKey,
  refreshActiveProfile,
  requestFreshSession
} from '@/store/profile'
import { $connection } from '@/store/session'

const LAST_PROFILE_STORAGE_KEY = 'hermes.desktop.lastProfileByConnection'

// The local pool ("This device") is keyed under a reserved name that can never
// collide with a real connection id. A connection id is user/migration
// controlled (`import`, `add`, a hand-edited registry) and could in principle
// be `local`; keeping the pool's remembered profile in a disjoint slot means
// the two writers can never stomp each other.
const LOCAL_POOL_KEY = '__local_pool__'

export const $connectionsRegistry = atom<DesktopConnectionsRegistry | null>(null)

// Use only the resolved descriptor identity Electron publishes. `primary`
// means the registry default, not necessarily the source this window is using;
// guessing it here would paint the wrong source as active for an unmatched v1
// route or while a legacy main is still resolving the descriptor.
export const $activeConnectionId = computed($connection, connection => connection?.connectionId ?? null)

export const $hasMultipleConnections = computed(
  $connectionsRegistry,
  registry => (registry?.connections.length ?? 0) > 1
)

const $lastProfileByConnection = atom<Record<string, string>>(storedStringRecord(LAST_PROFILE_STORAGE_KEY))
let pendingTarget: null | string = null
let restoreAttempted = false
let switchRevision = 0

export const $pendingConnectionId = atom<null | string>(null)

$lastProfileByConnection.subscribe(value => persistStringRecord(LAST_PROFILE_STORAGE_KEY, value))

const $activeConnectionProfile = computed(
  [$activeConnectionId, $activeGatewayProfile, $connection, $pendingConnectionId],
  (connectionId, profile, connection, pendingConnectionId) => ({
    connectionId,
    descriptorProfile: normalizeProfileKey(connection?.profile),
    profile: normalizeProfileKey(profile),
    registryScoped: connection?.registryScoped === true,
    mode: connection?.mode,
    // Distinguish "primary backend has no profile field" (undefined → record
    // the live gateway profile directly) from a pooled secondary or a stale
    // startup descriptor that DOES carry a profile but must still match.
    hasDescriptorProfile: connection?.profile != null,
    pendingConnectionId
  })
)

// Remember one profile per source, so switching machines is a re-home rather
// than a reset to `default`. The map is local UI preference only; Electron
// remains the authority for the connection registry and all secrets.
$activeConnectionProfile.subscribe(({ connectionId, descriptorProfile, profile, registryScoped, mode, hasDescriptorProfile, pendingConnectionId }) => {
  // A local-pool switch carries no connectionId (legacy route), but it is
  // still "This device" — key it under a reserved name so switching back from
  // a remote source re-homes to the last-used local profile instead of
  // resetting to `default`. Remote sources key under their connectionId.
  const key = mode === 'local' ? LOCAL_POOL_KEY : connectionId

  if (!key) {
    // No source to attribute yet (the boot window before $connection resolves,
    // or a descriptor with neither a local mode nor an id).
    return
  }

  // A connection switch in flight leaves $activeGatewayProfile briefly naming
  // the TARGET (via onActiveRouteChanged) while $connection still describes the
  // previous source — recording then would write the wrong profile under the
  // old key (e.g. the local pool ← the remote's profile). Deriving pending into
  // this computed makes its true→false edge re-emit here, so the settled state
  // is recorded even when the reset lands after the last $connection update.
  // Profile switches (selectProfile) never set it, so they record immediately.
  if (pendingConnectionId) {
    return
  }

  if (mode !== 'local') {
    // Remote source: keep the full guard (registryScoped + descriptor match),
    // which also rejects a migrated v1 routing alias.
    if (!registryScoped || descriptorProfile !== profile) {
      return
    }
  } else if (hasDescriptorProfile && descriptorProfile !== profile) {
    // A pooled secondary (or a stale startup descriptor) carries a profile and
    // must still match. Only a profile-less PRIMARY backend records the live
    // gateway profile directly — its `profile` field is absent by design.
    return
  }

  if ($lastProfileByConnection.get()[key] === profile) {
    return
  }

  $lastProfileByConnection.set({ ...$lastProfileByConnection.get(), [key]: profile })
})

/** @internal Reset module-owned preferences and switch coordination for tests. */
export function _resetConnectionsForTests(): void {
  $lastProfileByConnection.set({})
  pendingTarget = null
  restoreAttempted = false
  switchRevision = 0
  $pendingConnectionId.set(null)
}

export function setConnectionsRegistry(registry: DesktopConnectionsRegistry): void {
  $connectionsRegistry.set(registry)
}

/** Refresh the renderer cache from Electron's local registry. No backend is contacted. */
export async function refreshConnectionsRegistry(): Promise<DesktopConnectionsRegistry | null> {
  const bridge = window.hermesDesktop?.connections

  if (!bridge) {
    return null
  }

  const registry = await bridge.list()
  setConnectionsRegistry(registry)

  return registry
}

async function rememberConnection(connectionId: string): Promise<void> {
  const setLastUsed = window.hermesDesktop?.connections?.setLastUsed

  if (!setLastUsed) {
    return
  }

  try {
    const result = await setLastUsed(connectionId)
    setConnectionsRegistry(result.registry)
  } catch {
    // The source is already usable. A read-only/full userData directory must
    // not turn a successful backend switch into a false connection failure.
  }
}

/**
 * Load the registry once for Sessions and restore the last successfully used
 * source. Later registry refreshes stay side-effect free, so editing Settings
 * in another window never changes the active workspace.
 */
export async function initializeConnectionsRegistry(): Promise<DesktopConnectionsRegistry | null> {
  const registry = await refreshConnectionsRegistry()

  if (!registry || restoreAttempted) {
    return registry
  }

  restoreAttempted = true

  const lastUsed = registry.connections.some(connection => connection.id === registry.lastUsed)
    ? registry.lastUsed
    : registry.primary

  const preferredId = registry.launchMode === 'last-used' ? lastUsed : registry.primary

  if (!preferredId) {
    return registry
  }

  if ($activeConnectionId.get() === preferredId) {
    await rememberConnection(preferredId)
  } else {
    await selectConnection(preferredId)
  }

  return $connectionsRegistry.get() ?? registry
}

/**
 * Re-home Sessions to one registered source, restoring that source's last
 * profile. Only the selected source is dialed; merely rendering the switcher
 * never probes or opens remote gateways.
 */
export async function selectConnection(connectionId: string): Promise<void> {
  const registry = $connectionsRegistry.get()
  const targetConnection = registry?.connections.find(connection => connection.id === connectionId)

  if (!registry || !targetConnection) {
    return
  }

  const currentConnectionId = $activeConnectionId.get()
  const currentProfile = normalizeProfileKey($activeGatewayProfile.get())
  const rememberKey = targetConnection.kind === 'local' ? LOCAL_POOL_KEY : connectionId
  const targetProfile = normalizeProfileKey($lastProfileByConnection.get()[rememberKey] ?? 'default')
  const targetKey = `${connectionId}::${targetProfile}`

  if (pendingTarget === targetKey) {
    return
  }

  const switching =
    pendingTarget !== null ||
    $showAllProfiles.get() ||
    currentConnectionId !== connectionId ||
    currentProfile !== targetProfile

  if (!switching) {
    await rememberConnection(connectionId)

    return
  }

  if (pendingTarget === null && currentConnectionId === connectionId && currentProfile === targetProfile) {
    $showAllProfiles.set(false)
    $newChatProfile.set(targetProfile)
    requestFreshSession()
    await rememberConnection(connectionId)

    return
  }

  const revision = ++switchRevision
  pendingTarget = targetKey
  $pendingConnectionId.set(connectionId)

  try {
    // Always use the explicit registry route. `local` must mean This device,
    // and a registry primary can differ from a legacy per-profile override.
    await ensureGatewayAgent(connectionId, targetProfile)

    if ($connection.get()?.connectionId !== connectionId) {
      throw new Error(`Connection "${targetConnection.label}" did not become active.`)
    }

    // A newer click owns the final refresh. Serialized gateway activation
    // already makes the latest source win; this guard also prevents an older
    // request from repainting its profile list after that newer activation.
    if (revision === switchRevision) {
      await rememberConnection(connectionId)
      wipeSessionListsForGatewaySwitch()
      $showAllProfiles.set(false)
      $newChatProfile.set(targetProfile)
      requestFreshSession()
      await refreshActiveProfile()
    }
  } catch (error) {
    if (revision === switchRevision) {
      throw error
    }
  } finally {
    if (revision === switchRevision) {
      pendingTarget = null
      $pendingConnectionId.set(null)
    }
  }
}
