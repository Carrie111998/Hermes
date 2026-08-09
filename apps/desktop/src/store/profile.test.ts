import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesConnection } from '@/global'
import type { ProfileInfo } from '@/types/hermes'

// Keep profile.ts's side-effecting imports inert: the gateway socket layer and
// the REST query client must not run for real in a unit test.
const ensureGatewayForProfile = vi.fn(async () => undefined)
const openGatewayForProfile = vi.fn(async (_profile: string) => undefined)
const activeGatewayTargetOptions = vi.fn(() => ({}))
const $gateway = atom<unknown>({ id: 'live-socket' })
const resetStarmapGraph = vi.fn()

vi.mock('@/store/gateway', () => ({ $gateway, activeGatewayTargetOptions, ensureGatewayForProfile, openGatewayForProfile }))
vi.mock('@/hermes', () => ({
  getProfiles: vi.fn(async () => ({ profiles: [] })),
  setApiRequestProfile: vi.fn()
}))
vi.mock('@/lib/query-client', () => ({ invalidateProfileScopedQueries: vi.fn() }))
vi.mock('@/store/starmap', () => ({ resetStarmapGraph }))

const {
  $activeGatewayProfile,
  $profiles,
  $freshSessionRequest,
  ensureGatewayProfile,
  prewarmBackend,
  prewarmProfileBackend,
  selectBackend,
  refreshProfiles,
  selectProfile,
  touchActiveGatewayBackend
} =
  await import('./profile')

const { $connection } = await import('./session')
const { invalidateProfileScopedQueries } = await import('@/lib/query-client')
const { getProfiles, setApiRequestProfile } = await import('@/hermes')

const profile = (name: string, isDefault = false): ProfileInfo => ({
  has_env: false,
  is_default: isDefault,
  model: null,
  name,
  path: `/tmp/hermes/${name}`,
  provider: null,
  skill_count: 0
})

const remoteConn = (over: Partial<HermesConnection> = {}): HermesConnection =>
  ({ baseUrl: 'https://hermes-roy.tail.ts.net', mode: 'remote', profile: 'vps-remote', ...over }) as HermesConnection

const localConn = (over: Partial<HermesConnection> = {}): HermesConnection =>
  ({ baseUrl: '', mode: 'local', profile: 'default', ...over }) as HermesConnection

const getConnection = vi.fn<
  (profile?: string | null, options?: { localOnly?: boolean; remoteOnly?: boolean }) => Promise<HermesConnection>
>()

const touchBackend = vi.fn(async () => ({ ok: true }))

async function flushMicrotasks(count = 4): Promise<void> {
  for (let index = 0; index < count; index += 1) {
    await Promise.resolve()
  }
}

beforeEach(() => {
  getConnection.mockReset()
  ensureGatewayForProfile.mockClear()
  openGatewayForProfile.mockClear()
  activeGatewayTargetOptions.mockReset()
  activeGatewayTargetOptions.mockReturnValue({})
  $gateway.set({ id: 'live-socket' })
  $activeGatewayProfile.set('default')
  $connection.set(localConn())
  $profiles.set([])
  $freshSessionRequest.set(0)
  vi.mocked(getProfiles).mockReset()
  vi.mocked(getProfiles).mockResolvedValue({ profiles: [] })
  vi.mocked(setApiRequestProfile).mockClear()
  touchBackend.mockClear()
  vi.stubGlobal('window', { hermesDesktop: { getConnection, touchBackend } })
  vi.mocked(invalidateProfileScopedQueries).mockClear()
  resetStarmapGraph.mockClear()
})

afterEach(() => {
  vi.unstubAllGlobals()
  $connection.set(null)
})

describe('ensureGatewayProfile → $connection sync (#46651)', () => {
  it('refreshes $connection to the remote descriptor when activating a remote pool profile', async () => {
    // Regression: the primary window backend is local, so $connection.mode is
    // "local". Activating the remote profile must flip it to "remote" — without
    // this, image attach uses path-based image.attach against the remote
    // gateway ("image not found: C:\\…") instead of image.attach_bytes.
    getConnection.mockResolvedValue(remoteConn())

    await ensureGatewayProfile('vps-remote')

    expect(ensureGatewayForProfile).toHaveBeenCalledWith('vps-remote')
    expect(getConnection).toHaveBeenCalledWith('vps-remote')
    expect($connection.get()?.mode).toBe('remote')
    expect($connection.get()?.profile).toBe('vps-remote')
  })

  it('resyncs $connection back to local when returning to the default profile', async () => {
    $activeGatewayProfile.set('vps-remote')
    $connection.set(remoteConn())
    getConnection.mockResolvedValue(localConn())

    await ensureGatewayProfile('default')

    expect(getConnection).toHaveBeenCalledWith('default')
    expect($connection.get()?.mode).toBe('local')
  })

  it('routes the local default explicitly when a remote default is active', async () => {
    $activeGatewayProfile.set('default')
    $connection.set(remoteConn({ profile: 'default' }))
    getConnection.mockResolvedValue(localConn())

    await ensureGatewayProfile('default', { localOnly: true })

    expect(ensureGatewayForProfile).toHaveBeenCalledWith('default', { localOnly: true })
    expect(getConnection).toHaveBeenCalledWith('default', { localOnly: true })
    expect($connection.get()?.mode).toBe('local')
  })

  it('routes the remote default explicitly when a local default is active', async () => {
    $activeGatewayProfile.set('default')
    $connection.set(localConn())
    getConnection.mockResolvedValue(remoteConn({ profile: 'default' }))

    await ensureGatewayProfile('default', { remoteOnly: true })

    expect(ensureGatewayForProfile).toHaveBeenCalledWith('default', { remoteOnly: true })
    expect(getConnection).toHaveBeenCalledWith('default', { remoteOnly: true })
    expect($connection.get()?.mode).toBe('remote')
    expect($connection.get()?.profile).toBe('default')
    expect(setApiRequestProfile).toHaveBeenLastCalledWith('default', { remoteOnly: true })
  })

  it('selects the local default instead of no-oping on a remote default with the same name', () => {
    $activeGatewayProfile.set('default')
    $connection.set(remoteConn({ profile: 'default' }))

    selectProfile('default')

    expect(ensureGatewayForProfile).toHaveBeenCalledWith('default', { localOnly: true })
  })

  it('leaves the prior connection intact when the descriptor fetch fails', async () => {
    getConnection.mockRejectedValue(new Error('backend unreachable'))

    await ensureGatewayProfile('vps-remote')

    // Best-effort: boot/reconnect resyncs later; we must not null it out here.
    expect($connection.get()?.mode).toBe('local')
  })

  it('does not churn $connection when the target is already the active profile', async () => {
    $activeGatewayProfile.set('vps-remote')
    $connection.set(remoteConn())

    await ensureGatewayProfile('vps-remote')

    expect(getConnection).not.toHaveBeenCalled()
    expect(ensureGatewayForProfile).not.toHaveBeenCalled()
    expect($connection.get()?.mode).toBe('remote')
  })

  it('does not no-op a remote default target just because the local default is active', async () => {
    $activeGatewayProfile.set('default')
    $connection.set(localConn())
    getConnection.mockResolvedValue(remoteConn({ profile: 'default' }))

    await ensureGatewayProfile('default', { remoteOnly: true })

    expect(getConnection).toHaveBeenCalledWith('default', { remoteOnly: true })
    expect(ensureGatewayForProfile).toHaveBeenCalledWith('default', { remoteOnly: true })
  })
})

describe('selectBackend', () => {
  it('selects the local root explicitly from a remote root', () => {
    $activeGatewayProfile.set('default')
    $connection.set(remoteConn({ profile: 'default' }))

    selectBackend('local')

    expect(ensureGatewayForProfile).toHaveBeenCalledWith('default', { localOnly: true })
  })

  it('selects the remote root explicitly from a local root', () => {
    $activeGatewayProfile.set('default')
    $connection.set(localConn())

    selectBackend('remote')

    expect(ensureGatewayForProfile).toHaveBeenCalledWith('default', { remoteOnly: true })
  })

  it('requests a fresh session only when the backend target changes', async () => {
    $activeGatewayProfile.set('default')
    $connection.set(localConn())

    selectBackend('local')
    expect($freshSessionRequest.get()).toBe(0)

    selectBackend('remote')
    await flushMicrotasks()

    expect($freshSessionRequest.get()).toBe(1)
  })

  it('leaves the fresh-session request unchanged when the background switch fails', async () => {
    $activeGatewayProfile.set('default')
    $connection.set(localConn())
    ensureGatewayForProfile.mockRejectedValueOnce(new Error('remote unavailable'))

    selectBackend('remote')
    await flushMicrotasks()

    expect($freshSessionRequest.get()).toBe(0)
  })

  it('leaves the active connection in place when the background switch fails', async () => {
    $activeGatewayProfile.set('default')
    $connection.set(localConn())
    ensureGatewayForProfile.mockRejectedValueOnce(new Error('remote unavailable'))

    selectBackend('remote')
    await flushMicrotasks()

    expect($connection.get()?.mode).toBe('local')
  })
})

describe('profile-scoped cache invalidation', () => {
  it('drops the memory graph cache when the active gateway profile changes', () => {
    $activeGatewayProfile.set('coder')

    expect(invalidateProfileScopedQueries).toHaveBeenCalled()
    expect(resetStarmapGraph).toHaveBeenCalledTimes(1)
  })
})

describe('prewarmProfileBackend (hover-intent pool spawn)', () => {
  it('opens the local root without activating it', () => {
    $activeGatewayProfile.set('default')
    $connection.set(remoteConn({ profile: 'default' }))

    prewarmBackend('local')

    expect(openGatewayForProfile).toHaveBeenCalledWith('default', { localOnly: true })
    expect(ensureGatewayForProfile).not.toHaveBeenCalled()
  })

  it('opens the remote root without activating it', () => {
    $activeGatewayProfile.set('default')
    $connection.set(localConn())

    prewarmBackend('remote')

    expect(openGatewayForProfile).toHaveBeenCalledWith('default', { remoteOnly: true })
    expect(ensureGatewayForProfile).not.toHaveBeenCalled()
  })

  it('throttles repeat pre-warms for the same backend target within the interval', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2100-01-01T00:00:00.000Z'))
    $connection.set(remoteConn({ profile: 'default' }))

    prewarmBackend('local')
    prewarmBackend('local')

    expect(openGatewayForProfile).toHaveBeenNthCalledWith(1, 'default', { localOnly: true })
    expect(openGatewayForProfile).toHaveBeenCalledTimes(1)

    vi.useRealTimers()
  })

  it('opens the gateway (spawn + connect, no activation) for a non-active profile', () => {
    prewarmProfileBackend('warm-basic')

    expect(openGatewayForProfile).toHaveBeenCalledWith('warm-basic')
    // Pre-warm must never activate — that's the click's job.
    expect(ensureGatewayForProfile).not.toHaveBeenCalled()
  })

  it('skips the profile the gateway is already on', () => {
    $activeGatewayProfile.set('warm-active')

    prewarmProfileBackend('warm-active')

    expect(openGatewayForProfile).not.toHaveBeenCalled()
  })

  it('throttles repeat pre-warms for the same profile within the interval', () => {
    prewarmProfileBackend('warm-throttle-a')
    prewarmProfileBackend('warm-throttle-a')
    prewarmProfileBackend('warm-throttle-b')

    const calls = openGatewayForProfile.mock.calls.map(([name]) => name)
    expect(calls.filter(name => name === 'warm-throttle-a')).toHaveLength(1)
    expect(calls.filter(name => name === 'warm-throttle-b')).toHaveLength(1)
  })

  it('swallows spawn failures — error UX belongs to the real switch', () => {
    openGatewayForProfile.mockRejectedValueOnce(new Error('spawn failed'))

    expect(() => prewarmProfileBackend('warm-failing')).not.toThrow()
  })
})

describe('touchActiveGatewayBackend', () => {
  it('passes the explicit remote root target through the desktop touch API', async () => {
    $activeGatewayProfile.set('default')
    activeGatewayTargetOptions.mockReturnValue({ remoteOnly: true })

    touchActiveGatewayBackend()
    await Promise.resolve()

    expect(touchBackend).toHaveBeenCalledWith('default', { remoteOnly: true })
  })
})

describe('refreshProfiles shared rail list (#49289)', () => {
  it('merges the local catalog into the active remote catalog', async () => {
    $activeGatewayProfile.set('remote-agent')
    vi.mocked(getProfiles)
      .mockResolvedValueOnce({ profiles: [profile('default', true), profile('remote-agent')] })
      .mockResolvedValueOnce({ profiles: [profile('default', true), profile('local-worker')] })

    await refreshProfiles()

    expect(getProfiles).toHaveBeenNthCalledWith(1, 'remote-agent')
    expect(getProfiles).toHaveBeenNthCalledWith(2, 'default', { localOnly: true })
    expect($profiles.get().map(item => item.name)).toEqual(['default', 'local-worker', 'remote-agent'])
  })

  it('loads the local catalog when the active remote backend also uses default', async () => {
    $activeGatewayProfile.set('default')
    $connection.set(remoteConn({ profile: 'default' }))
    vi.mocked(getProfiles)
      .mockResolvedValueOnce({ profiles: [profile('default', true), profile('remote-worker')] })
      .mockResolvedValueOnce({ profiles: [profile('default', true), profile('local-worker')] })

    await refreshProfiles()

    expect(getProfiles).toHaveBeenNthCalledWith(1, null)
    expect(getProfiles).toHaveBeenNthCalledWith(2, 'default', { localOnly: true })
    expect($profiles.get().map(item => item.name)).toEqual(['default', 'local-worker', 'remote-worker'])
  })

  it('removes a deleted profile from the shared $profiles cache after Manage Profiles refreshes', async () => {
    $profiles.set([profile('default', true), profile('test1')])
    vi.mocked(getProfiles).mockResolvedValueOnce({ profiles: [profile('default', true)] })

    await refreshProfiles()

    expect($profiles.get().map(profile => profile.name)).toEqual(['default'])
  })

  it('leaves the shared $profiles cache intact when the refresh fails', async () => {
    $profiles.set([profile('default', true), profile('test1')])
    vi.mocked(getProfiles).mockRejectedValueOnce(new Error('backend unavailable'))

    await expect(refreshProfiles()).rejects.toThrow('backend unavailable')

    expect($profiles.get().map(profile => profile.name)).toEqual(['default', 'test1'])
  })
})
