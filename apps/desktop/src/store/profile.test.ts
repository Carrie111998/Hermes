import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesConnection } from '@/global'
import type { ProfileInfo } from '@/types/hermes'

// Keep profile.ts's side-effecting imports inert: the gateway socket layer and
// the REST query client must not run for real in a unit test.
const ensureGatewayForProfile = vi.fn(async () => undefined)
const openGatewayForProfile = vi.fn(async (_profile: string) => undefined)
const $gateway = atom<unknown>({ id: 'live-socket' })
const resetStarmapGraph = vi.fn()

vi.mock('@/store/gateway', () => ({ $gateway, ensureGatewayForProfile, openGatewayForProfile }))
vi.mock('@/hermes', () => ({
  getProfiles: vi.fn(async () => ({ profiles: [] })),
  setApiRequestProfile: vi.fn(),
  STARTUP_REQUEST_TIMEOUT_MS: 10_000
}))
vi.mock('@/lib/query-client', () => ({ invalidateProfileScopedQueries: vi.fn() }))
vi.mock('@/store/starmap', () => ({ resetStarmapGraph }))

const {
  $activeGatewayProfile,
  $activeProfile,
  $primaryDesktopProfileState,
  $profiles,
  ensureGatewayProfile,
  prewarmProfileBackend,
  refreshPrimaryProfile,
  refreshProfiles,
  selectProfile,
  switchProfile
} = await import('./profile')

const { $connection } = await import('./session')
const { invalidateProfileScopedQueries } = await import('@/lib/query-client')
const { getProfiles } = await import('@/hermes')

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

const getConnection = vi.fn<(profile?: string | null) => Promise<HermesConnection>>()
const getPrimaryProfile = vi.fn<() => Promise<{ profile: string | null }>>()
const setPrimaryProfile = vi.fn<(profile: string) => Promise<{ profile: string | null }>>()
const desktopApi = vi.fn<(request: { path: string; timeoutMs?: number }) => Promise<unknown>>()

beforeEach(() => {
  getConnection.mockReset()
  getPrimaryProfile.mockReset()
  getPrimaryProfile.mockResolvedValue({ profile: 'default' })
  setPrimaryProfile.mockReset()
  setPrimaryProfile.mockResolvedValue({ profile: 'default' })
  desktopApi.mockReset()
  desktopApi.mockResolvedValue({ active: 'default', current: 'default' })
  ensureGatewayForProfile.mockClear()
  openGatewayForProfile.mockClear()
  $gateway.set({ id: 'live-socket' })
  $activeProfile.set('default')
  $primaryDesktopProfileState.set({ current: 'default', persisted: 'default' })
  $activeGatewayProfile.set('default')
  $connection.set(localConn())
  $profiles.set([])
  vi.stubGlobal('window', {
    hermesDesktop: {
      api: desktopApi,
      getConnection,
      profile: { get: getPrimaryProfile, set: setPrimaryProfile }
    }
  })
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
})

describe('profile-scoped cache invalidation', () => {
  it('drops the memory graph cache when the active gateway profile changes', () => {
    $activeGatewayProfile.set('coder')

    expect(invalidateProfileScopedQueries).toHaveBeenCalled()
    expect(resetStarmapGraph).toHaveBeenCalledTimes(1)
  })
})

describe('prewarmProfileBackend (hover-intent pool spawn)', () => {
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

describe('refreshProfiles shared rail list (#49289)', () => {
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

describe('primary Desktop profile refresh', () => {
  it('reads the actual primary backend independently from workspace routing', async () => {
    $activeGatewayProfile.set('workspace')
    desktopApi.mockResolvedValueOnce({ active: 'dev', current: 'dev' })

    await refreshPrimaryProfile()

    expect(desktopApi).toHaveBeenCalledWith(expect.objectContaining({ path: '/api/profiles/active' }))
    expect(desktopApi.mock.calls[0]?.[0]).not.toHaveProperty('profile')
    expect($activeProfile.get()).toBe('dev')
    expect($primaryDesktopProfileState.get()).toEqual({ current: 'dev', persisted: 'default' })
    expect(getPrimaryProfile).toHaveBeenCalledOnce()
  })

  it('falls back to the persisted primary while the backend is unavailable', async () => {
    desktopApi.mockRejectedValueOnce(new Error('backend starting'))
    getPrimaryProfile.mockResolvedValueOnce({ profile: 'dev' })

    await refreshPrimaryProfile()

    expect($activeProfile.get()).toBe('dev')
    expect($primaryDesktopProfileState.get()).toEqual({ current: null, persisted: 'dev' })
  })
})

describe('persisted Desktop primary profile switching (#85991)', () => {
  it('persists named to default through the Desktop profile IPC', async () => {
    $activeProfile.set('dev')
    $primaryDesktopProfileState.set({ current: 'dev', persisted: 'dev' })
    getPrimaryProfile.mockResolvedValueOnce({ profile: 'dev' })

    await switchProfile('default')

    expect(setPrimaryProfile).toHaveBeenCalledWith('default')
  })

  it('persists default to a named primary profile through the same IPC', async () => {
    await switchProfile('dev')

    expect(setPrimaryProfile).toHaveBeenCalledWith('dev')
  })

  it('does not invoke the IPC for the already-primary profile', async () => {
    $activeProfile.set('dev')
    $primaryDesktopProfileState.set({ current: 'dev', persisted: 'dev' })
    getPrimaryProfile.mockResolvedValueOnce({ profile: 'dev' })

    await switchProfile('dev')

    expect(setPrimaryProfile).not.toHaveBeenCalled()
  })

  it('keeps a failed re-home retryable when persistence already succeeded', async () => {
    setPrimaryProfile.mockImplementationOnce(async profile => {
      getPrimaryProfile.mockResolvedValue({ profile })
      throw new Error('backend did not stop')
    })

    await expect(switchProfile('dev')).rejects.toThrow('backend did not stop')
    expect($activeProfile.get()).toBe('default')
    expect($primaryDesktopProfileState.get()).toEqual({ current: 'default', persisted: 'dev' })

    setPrimaryProfile.mockResolvedValueOnce({ profile: 'dev' })
    await switchProfile('dev')
    expect(setPrimaryProfile).toHaveBeenCalledTimes(2)
  })

  it('keeps a persisted target retryable when the running primary is unconfirmed', async () => {
    $primaryDesktopProfileState.set({ current: null, persisted: 'default' })
    getPrimaryProfile.mockResolvedValueOnce({ profile: 'default' })

    await switchProfile('default')

    expect(setPrimaryProfile).toHaveBeenCalledWith('default')
  })

  it('keeps an actual workspace switch independent from persisted primary state', async () => {
    $activeProfile.set('dev')
    $activeGatewayProfile.set('dev')
    getConnection.mockResolvedValue(localConn())

    selectProfile('default')

    await vi.waitFor(() => expect(ensureGatewayForProfile).toHaveBeenCalledWith('default'))
    expect(setPrimaryProfile).not.toHaveBeenCalled()
  })
})
