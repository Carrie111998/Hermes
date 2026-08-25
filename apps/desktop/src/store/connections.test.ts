import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { DesktopConnectionsRegistry } from '@/global'

const $activeGatewayProfile = atom('default')
const $newChatProfile = atom<null | string>(null)
const $newChatRoute = atom<null | { connectionId: string; profile: string }>(null)
const $showAllProfiles = atom(false)

const $connection = atom<null | {
  connectionId?: string
  mode?: 'local' | 'remote'
  profile?: string
  registryScoped?: boolean
}>(null)

const ensureGatewayAgent = vi.fn(async (_connectionId: null | string, _profile: string): Promise<void> => undefined)
const beginGatewayRouteIntent = vi.fn(() => ++routeIntentGeneration)
const isCurrentGatewayRouteIntent = vi.fn((generation: number) => generation === routeIntentGeneration)
const refreshActiveProfile = vi.fn(async () => undefined)
const requestFreshSession = vi.fn()
const wipeSessionListsForGatewaySwitch = vi.fn()
let routeIntentGeneration = 0

const selectProfile = vi.fn((profile: string) => {
  beginGatewayRouteIntent()
  $newChatRoute.set({ connectionId: 'profile-source', profile })
})

vi.mock('@/store/session', () => ({ $connection }))
vi.mock('@/store/gateway-switch', () => ({ wipeSessionListsForGatewaySwitch }))
vi.mock('@/store/profile', () => ({
  $activeGatewayProfile,
  $newChatProfile,
  $newChatRoute,
  $showAllProfiles,
  beginGatewayRouteIntent,
  ensureGatewayAgent,
  isCurrentGatewayRouteIntent,
  normalizeProfileKey: (name: null | string | undefined) => (name ?? '').trim() || 'default',
  refreshActiveProfile,
  requestFreshSession,
  selectProfile
}))

const {
  $activeConnectionId,
  $connectionsRegistry,
  $pendingConnectionId,
  initializeConnectionsRegistry,
  refreshConnectionsRegistry,
  _resetConnectionsForTests,
  selectConnection,
  setConnectionsRegistry
} = await import('./connections')

const registry: DesktopConnectionsRegistry = {
  connections: [
    { id: 'local', kind: 'local', label: 'This device', tokenPreview: null, tokenSet: false },
    { id: 'homelab', kind: 'remote', label: 'Homelab', tokenPreview: '...abc', tokenSet: true },
    { id: 'work-vps', kind: 'remote', label: 'Work VPS', tokenPreview: '...xyz', tokenSet: true }
  ],
  primary: 'local',
  secureTokenStorage: true,
  version: 2
}

const list = vi.fn(async () => registry)
const setLastUsed = vi.fn(async (id: string) => ({ ok: true, registry: { ...registry, lastUsed: id } }))

beforeEach(() => {
  localStorage.clear()
  _resetConnectionsForTests()
  $connectionsRegistry.set(null)
  $connection.set(null)
  $activeGatewayProfile.set('default')
  $newChatProfile.set(null)
  $newChatRoute.set(null)
  $showAllProfiles.set(false)
  routeIntentGeneration = 0
  beginGatewayRouteIntent.mockClear()
  isCurrentGatewayRouteIntent.mockClear()
  selectProfile.mockClear()
  ensureGatewayAgent.mockReset()
  ensureGatewayAgent.mockImplementation(async (connectionId, profile) => {
    $connection.set({
      connectionId: connectionId ?? undefined,
      mode: connectionId === 'local' ? 'local' : 'remote',
      profile,
      registryScoped: true
    })
    $activeGatewayProfile.set(profile)
  })
  refreshActiveProfile.mockClear()
  requestFreshSession.mockClear()
  wipeSessionListsForGatewaySwitch.mockClear()
  list.mockClear()
  setLastUsed.mockClear()
  vi.stubGlobal('window', { hermesDesktop: { connections: { list, setLastUsed } }, localStorage })
})

afterEach(() => vi.unstubAllGlobals())

describe('connection registry cache', () => {
  it('loads only Electron local registry state', async () => {
    await refreshConnectionsRegistry()

    expect(list).toHaveBeenCalledTimes(1)
    expect($connectionsRegistry.get()).toEqual(registry)
    expect($activeConnectionId.get()).toBeNull()
  })

  it('restores the last-used source once when that launch mode is enabled', async () => {
    list.mockResolvedValueOnce({ ...registry, lastUsed: 'homelab', launchMode: 'last-used' })
    $connection.set({ connectionId: 'local', mode: 'local' })

    await initializeConnectionsRegistry()
    await initializeConnectionsRegistry()

    expect(ensureGatewayAgent).toHaveBeenCalledTimes(1)
    expect(ensureGatewayAgent).toHaveBeenCalledWith('homelab', 'default')
    expect(setLastUsed).toHaveBeenCalledWith('homelab')
  })

  it('preserves the established Primary-source launch behavior by default', async () => {
    list.mockResolvedValueOnce({ ...registry, lastUsed: 'homelab', launchMode: 'primary' })
    $connection.set({ connectionId: 'local', mode: 'local' })

    await initializeConnectionsRegistry()

    expect(ensureGatewayAgent).not.toHaveBeenCalled()
  })

  it('restores a remote registry primary through its exact connection id', async () => {
    list.mockResolvedValueOnce({ ...registry, primary: 'homelab', launchMode: 'primary' })
    $connection.set({ connectionId: 'local', mode: 'local' })

    await initializeConnectionsRegistry()

    expect(ensureGatewayAgent).toHaveBeenCalledTimes(1)
    expect(ensureGatewayAgent).toHaveBeenCalledWith('homelab', 'default')
    expect(setLastUsed).toHaveBeenCalledWith('homelab')
  })

  it('uses only the resolved descriptor identity for the active gateway', () => {
    setConnectionsRegistry({ ...registry, primary: 'homelab' })
    $connection.set({ connectionId: 'work-vps', mode: 'remote' })
    expect($activeConnectionId.get()).toBe('work-vps')

    $connection.set({ mode: 'remote' })
    expect($activeConnectionId.get()).toBeNull()

    $connection.set({ connectionId: 'work-vps', mode: 'remote' })
    expect($activeConnectionId.get()).toBe('work-vps')
  })
})

describe('selectConnection', () => {
  it('dials a secondary source and starts a fresh source-scoped draft', async () => {
    setConnectionsRegistry(registry)
    $connection.set({ connectionId: 'local', mode: 'local' })

    await selectConnection('homelab')

    expect(ensureGatewayAgent).toHaveBeenCalledWith('homelab', 'default')
    expect(requestFreshSession).toHaveBeenCalledTimes(1)
    expect(wipeSessionListsForGatewaySwitch).toHaveBeenCalledTimes(1)
    expect($newChatProfile.get()).toBe('default')
    expect($newChatRoute.get()).toEqual({ connectionId: 'homelab', profile: 'default' })
    expect(refreshActiveProfile).toHaveBeenCalledTimes(1)
    expect(setLastUsed).toHaveBeenCalledWith('homelab')
  })

  it('does not reset or dial when the active source/profile is selected again', async () => {
    setConnectionsRegistry(registry)
    $connection.set({ connectionId: 'local', mode: 'local' })

    await selectConnection('local')

    expect(ensureGatewayAgent).not.toHaveBeenCalled()
    expect(requestFreshSession).not.toHaveBeenCalled()
  })

  it('uses an explicit local id when This device is not primary', async () => {
    setConnectionsRegistry({ ...registry, primary: 'homelab' })
    $connection.set({ connectionId: 'homelab', mode: 'remote' })

    await selectConnection('local')

    expect(ensureGatewayAgent).toHaveBeenCalledWith('local', 'default')
    expect($newChatRoute.get()).toEqual({ connectionId: 'local', profile: 'default' })
  })

  it('replaces a remote A draft owner when switching to the local source before Send', async () => {
    setConnectionsRegistry(registry)
    $connection.set({ connectionId: 'homelab', mode: 'remote', profile: 'default', registryScoped: true })
    $newChatRoute.set({ connectionId: 'homelab', profile: 'publisher' })

    await selectConnection('local')

    expect($newChatRoute.get()).toEqual({ connectionId: 'local', profile: 'default' })
  })

  it('replaces a remote A draft owner when switching to remote B before Send', async () => {
    setConnectionsRegistry(registry)
    $connection.set({ connectionId: 'homelab', mode: 'remote', profile: 'default', registryScoped: true })
    $newChatRoute.set({ connectionId: 'homelab', profile: 'shared-name' })

    await selectConnection('work-vps')

    expect($newChatRoute.get()).toEqual({ connectionId: 'work-vps', profile: 'default' })
  })

  it('fails closed when a stale remembered profile falls back on the target source', async () => {
    setConnectionsRegistry(registry)
    // Seed the per-source preference from a previously valid local descriptor,
    // then move the live gateway to another source before selecting local.
    $connection.set({ connectionId: 'local', mode: 'local', profile: 'stale-local-only', registryScoped: true })
    $activeGatewayProfile.set('stale-local-only')
    $connection.set({ connectionId: 'homelab', mode: 'remote', profile: 'default', registryScoped: true })
    $activeGatewayProfile.set('default')
    ensureGatewayAgent.mockImplementationOnce(async connectionId => {
      $connection.set({
        connectionId: connectionId ?? undefined,
        mode: 'local',
        profile: 'default',
        registryScoped: true
      })
      $activeGatewayProfile.set('default')
    })

    await expect(selectConnection('local')).rejects.toThrow(/profile "stale-local-only"/i)
    expect(requestFreshSession).not.toHaveBeenCalled()
    expect($newChatRoute.get()).toBeNull()
  })

  it('lets a later source choice win while an earlier dial is still pending', async () => {
    let releaseHomelab!: () => void

    const homelabGate = new Promise<void>(resolve => {
      releaseHomelab = resolve
    })

    setConnectionsRegistry(registry)
    $connection.set({ connectionId: 'local', mode: 'local' })
    ensureGatewayAgent.mockImplementationOnce(async () => {
      await homelabGate
      $connection.set({ connectionId: 'homelab', mode: 'remote' })
    })
    ensureGatewayAgent.mockImplementationOnce(async () => {
      await homelabGate
      $connection.set({ connectionId: 'local', mode: 'local' })
    })

    const openHomelab = selectConnection('homelab')
    await Promise.resolve()
    const stayLocal = selectConnection('local')

    releaseHomelab()
    await Promise.all([openHomelab, stayLocal])

    expect(ensureGatewayAgent.mock.calls).toEqual([
      ['homelab', 'default'],
      ['local', 'default']
    ])
    // Only the latest intent repaints the profile list.
    expect(refreshActiveProfile).toHaveBeenCalledTimes(1)
    expect(wipeSessionListsForGatewaySwitch).toHaveBeenCalledTimes(1)
  })

  it('does not let a slow source switch overwrite a newer profile route intent', async () => {
    let releaseHomelab!: () => void
    const homelabGate = new Promise<void>(resolve => {
      releaseHomelab = resolve
    })

    setConnectionsRegistry(registry)
    $connection.set({ connectionId: 'local', mode: 'local', profile: 'default', registryScoped: true })
    ensureGatewayAgent.mockImplementationOnce(async () => {
      await homelabGate
      $connection.set({ connectionId: 'homelab', mode: 'remote', profile: 'default', registryScoped: true })
      $activeGatewayProfile.set('default')
    })

    const slowSourceSwitch = selectConnection('homelab')
    await Promise.resolve()

    selectProfile('newer-profile')
    releaseHomelab()
    await slowSourceSwitch

    expect($newChatRoute.get()).toEqual({ connectionId: 'profile-source', profile: 'newer-profile' })
    expect(requestFreshSession).not.toHaveBeenCalled()
  })

  it('restores the last profile used on each source', async () => {
    setConnectionsRegistry(registry)
    $connection.set({ connectionId: 'local', mode: 'local', profile: 'research', registryScoped: true })
    $activeGatewayProfile.set('research')
    $connection.set({ connectionId: 'homelab', mode: 'remote', registryScoped: true })
    $activeGatewayProfile.set('default')

    await selectConnection('local')

    expect(ensureGatewayAgent).toHaveBeenCalledWith('local', 'research')
  })

  it('does not remember a migrated v1 routing alias as a backend profile', async () => {
    setConnectionsRegistry(registry)
    $connection.set({ connectionId: 'homelab', mode: 'remote' })
    $activeGatewayProfile.set('legacy-homelab-alias')
    $connection.set({ connectionId: 'local', mode: 'local', registryScoped: true })

    await selectConnection('homelab')

    expect(ensureGatewayAgent).toHaveBeenCalledWith('homelab', 'default')
  })

  it('does not remember a stale startup profile under the resolved source', async () => {
    setConnectionsRegistry(registry)
    $connection.set({ connectionId: 'local', mode: 'local', profile: 'default', registryScoped: true })
    $activeGatewayProfile.set('work-agent')
    $connection.set({ connectionId: 'homelab', mode: 'remote', profile: 'default', registryScoped: true })
    $activeGatewayProfile.set('default')

    await selectConnection('local')

    expect(ensureGatewayAgent).toHaveBeenCalledWith('local', 'default')
  })

  it('keeps the current source usable when a dial fails', async () => {
    setConnectionsRegistry(registry)
    $connection.set({ connectionId: 'local', mode: 'local' })
    ensureGatewayAgent.mockRejectedValueOnce(new Error('offline'))

    await expect(selectConnection('homelab')).rejects.toThrow('offline')

    expect(requestFreshSession).not.toHaveBeenCalled()
    expect(wipeSessionListsForGatewaySwitch).not.toHaveBeenCalled()
    expect($newChatProfile.get()).toBeNull()
    expect($pendingConnectionId.get()).toBeNull()
    expect(setLastUsed).not.toHaveBeenCalled()
  })

  it('boot-time restore leaves "All profiles" browse mode on (#93197)', async () => {
    // Fresh boot: nothing active yet, registry restores last-used. The
    // persisted showAllProfiles=true must survive the silent restore.
    list.mockResolvedValueOnce({ ...registry, lastUsed: 'homelab', launchMode: 'last-used' })
    $showAllProfiles.set(true)

    await initializeConnectionsRegistry()

    expect(ensureGatewayAgent).toHaveBeenCalledWith('homelab', 'default')
    expect($showAllProfiles.get()).toBe(true)
  })

  it('a user-initiated source switch still collapses "All profiles"', async () => {
    setConnectionsRegistry(registry)
    $connection.set({ connectionId: 'local', mode: 'local' })
    $showAllProfiles.set(true)

    await selectConnection('homelab')

    expect(ensureGatewayAgent).toHaveBeenCalledWith('homelab', 'default')
    expect($showAllProfiles.get()).toBe(false)
  })

  it('never re-homes a live connection the registry cannot name', async () => {
    // A window connected through the legacy v1 route carries an unqualified
    // descriptor, so resolvedConnectionId — and therefore $activeConnectionId
    // — is null. Restoring the registry primary over it would re-home a
    // working remote onto local a few seconds after boot.
    list.mockResolvedValueOnce({ ...registry, launchMode: 'primary', primary: 'local' })
    $connection.set({ mode: 'remote', profile: 'default', registryScoped: false })

    await initializeConnectionsRegistry()

    expect(ensureGatewayAgent).not.toHaveBeenCalled()
    expect(wipeSessionListsForGatewaySwitch).not.toHaveBeenCalled()
    expect($connection.get()?.mode).toBe('remote')
  })
})
