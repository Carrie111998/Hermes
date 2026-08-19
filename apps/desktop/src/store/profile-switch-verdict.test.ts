import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesConnection } from '@/global'

// A profile switch that DECLINES to publish must say so (#89586).
//
// `prepareGatewayForProfile` returns a thunk whose boolean is the whole answer
// to "did this activation actually land": it is false when the entry was torn
// down mid-dial, when a newer activation superseded this one's epoch, or when
// an eviction re-pointed the active key at the primary while this switch was
// awaiting its socket. `ensureGatewayProfile` called that thunk inside a
// `batch()` callback, ignored the result, and resolved successfully - so a
// switch that changed nothing was indistinguishable, to every caller and to
// the user, from one that worked. The profile rail's `void
// ensureGatewayProfile(target)` had nothing to react to, the empty `.catch()`
// printed nothing, and the click was a silent no-op.
//
// These tests pin the verdict rather than the mechanism: whatever makes an
// activation decline, the caller is told.

const INITIAL_GATEWAY = { id: 'live-socket' }
const PROFILE_GATEWAY = { id: 'profile-socket' }

const $gateway = atom<unknown>(INITIAL_GATEWAY)

// Publishing thunk: moves $gateway the way a real activation would, so a test
// asserting "nothing was published" is checking observable state and not just
// a spy call count.
const activateProfile = vi.fn(() => {
  $gateway.set(PROFILE_GATEWAY)

  return true
})

// Declining thunk: the shape `prepareGatewayForProfile` returns when its
// entry is gone or its epoch was superseded. It publishes NOTHING.
const declineProfile = vi.fn(() => false)

const prepareGatewayForProfile = vi.fn(async (_profile: string): Promise<() => boolean> => activateProfile)

const prepareGatewayForAgent = vi.fn(
  async (_connectionId: null | string, _profile: string): Promise<() => boolean> => activateProfile
)

const openGatewayForProfile = vi.fn(async (_profile: string) => undefined)

vi.mock('@/store/gateway', () => ({
  $gateway,
  openGatewayForProfile,
  prepareGatewayForAgent,
  prepareGatewayForProfile
}))
vi.mock('@/hermes', () => ({
  getProfiles: vi.fn(async () => ({ profiles: [] })),
  setApiRequestProfile: vi.fn()
}))
vi.mock('@/lib/query-client', () => ({ invalidateProfileScopedQueries: vi.fn() }))
vi.mock('@/store/starmap', () => ({ resetStarmapGraph: vi.fn() }))

const { $activeGatewayProfile, ensureGatewayAgent, ensureGatewayProfile } = await import('./profile')
const { $connection } = await import('./session')

const localConn = (over: Partial<HermesConnection> = {}): HermesConnection =>
  ({ baseUrl: '', mode: 'local', profile: 'default', ...over }) as HermesConnection

const remoteConn = (over: Partial<HermesConnection> = {}): HermesConnection =>
  ({ baseUrl: 'https://homelab.invalid', mode: 'remote', profile: 'research', ...over }) as HermesConnection

const getConnection = vi.fn<(profile?: string | null) => Promise<HermesConnection>>()

const getConnectionFor =
  vi.fn<(payload: { connectionId?: null | string; profile?: null | string }) => Promise<HermesConnection>>()

beforeEach(() => {
  getConnection.mockReset()
  getConnection.mockResolvedValue(remoteConn())
  getConnectionFor.mockReset()
  getConnectionFor.mockResolvedValue(remoteConn())
  prepareGatewayForProfile.mockReset()
  prepareGatewayForProfile.mockResolvedValue(activateProfile)
  prepareGatewayForAgent.mockReset()
  prepareGatewayForAgent.mockResolvedValue(activateProfile)
  activateProfile.mockClear()
  declineProfile.mockClear()
  $gateway.set(INITIAL_GATEWAY)
  $activeGatewayProfile.set('default')
  $connection.set(localConn())
  vi.stubGlobal('window', { hermesDesktop: { getConnection, getConnectionFor } })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  $connection.set(null)
})

describe('a declined activation is reported, not swallowed', () => {
  it('resolves false and publishes nothing when the activation declines', async () => {
    // THE regression. Before: this resolved (void) exactly like a successful
    // switch, so the rail click looked identical to a working one.
    prepareGatewayForProfile.mockResolvedValue(declineProfile)

    const switched = await ensureGatewayProfile('research')

    expect(switched).toBe(false)
    expect(declineProfile).toHaveBeenCalledTimes(1)
    expect($activeGatewayProfile.get()).toBe('default')
    expect($gateway.get()).toBe(INITIAL_GATEWAY)
    expect($connection.get()?.mode).toBe('local')
  })

  it('resolves true and publishes the whole tuple when the activation lands', async () => {
    const switched = await ensureGatewayProfile('research')

    expect(switched).toBe(true)
    expect($activeGatewayProfile.get()).toBe('research')
    expect($gateway.get()).toBe(PROFILE_GATEWAY)
    expect($connection.get()?.mode).toBe('remote')
  })

  it('resolves false when the descriptor lookup rejects', async () => {
    getConnection.mockRejectedValue(new Error('bridge unreachable'))
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)

    const switched = await ensureGatewayProfile('research')

    expect(switched).toBe(false)
    expect($activeGatewayProfile.get()).toBe('default')
    expect($gateway.get()).toBe(INITIAL_GATEWAY)
    // Not silent: the empty catch is what made a failed switch unobservable
    // in the renderer console the reporter checked.
    expect(warn).toHaveBeenCalled()
  })

  it('reports a landed agent activation as true', async () => {
    // The mirror of the case below. Without it, an agent switch that never
    // recorded its success would still pass every other assertion here,
    // because `published` defaults to false and the declined cases expect
    // exactly that - the mutation proof caught this gap.
    const switched = await ensureGatewayAgent('homelab', 'research')

    expect(switched).toBe(true)
    expect($activeGatewayProfile.get()).toBe('research')
    expect($gateway.get()).toBe(PROFILE_GATEWAY)
  })

  it('reports a declined agent activation too', async () => {
    prepareGatewayForAgent.mockResolvedValue(declineProfile)

    const switched = await ensureGatewayAgent('homelab', 'research')

    expect(switched).toBe(false)
    expect($activeGatewayProfile.get()).toBe('default')
    expect($gateway.get()).toBe(INITIAL_GATEWAY)
  })
})

describe('a declined switch does not wedge the next one', () => {
  it('a later switch still runs after a declined one', async () => {
    // The switch mutex is a module-global promise. A decline must leave it
    // cleared, or "the retry never works" becomes true for the rest of the
    // session — which is what the reporter saw after the first failure.
    prepareGatewayForProfile.mockResolvedValueOnce(declineProfile)

    expect(await ensureGatewayProfile('research')).toBe(false)
    expect(await ensureGatewayProfile('research')).toBe(true)
    expect($activeGatewayProfile.get()).toBe('research')
  })

  it('a later switch still runs after a rejected descriptor lookup', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    getConnection.mockRejectedValueOnce(new Error('bridge unreachable'))

    expect(await ensureGatewayProfile('research')).toBe(false)
    expect(await ensureGatewayProfile('research')).toBe(true)
    expect($activeGatewayProfile.get()).toBe('research')
  })

  it('serialized callers behind an in-flight switch get its verdict, not a stale true', async () => {
    prepareGatewayForProfile.mockResolvedValue(declineProfile)

    const [first, second] = await Promise.all([
      ensureGatewayProfile('research'),
      ensureGatewayProfile('research')
    ])

    // Neither published, so neither may claim the gateway now serves the
    // target — the second call waits on the first and then re-checks.
    expect(first).toBe(false)
    expect(second).toBe(false)
    expect($activeGatewayProfile.get()).toBe('default')
  })
})

describe('the no-op fast paths report satisfaction, not failure', () => {
  it('an empty target is satisfied by whatever is active', async () => {
    expect(await ensureGatewayProfile(null)).toBe(true)
    expect(await ensureGatewayProfile('')).toBe(true)
    expect(await ensureGatewayProfile('   ')).toBe(true)
    expect(prepareGatewayForProfile).not.toHaveBeenCalled()
  })

  it('re-selecting the already-active profile is satisfied without a switch', async () => {
    expect(await ensureGatewayProfile('research')).toBe(true)
    prepareGatewayForProfile.mockClear()

    expect(await ensureGatewayProfile('research')).toBe(true)
    expect(prepareGatewayForProfile).not.toHaveBeenCalled()
  })

  it('a null connection id falls through to the profile seam and keeps its verdict', async () => {
    prepareGatewayForProfile.mockResolvedValue(declineProfile)

    expect(await ensureGatewayAgent(null, 'research')).toBe(false)
    expect(prepareGatewayForAgent).not.toHaveBeenCalled()
  })
})
