import { atom } from 'nanostores'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// Picking a profile must stay on the source the user is LOOKING at. $profiles
// is the active gateway's list, so a pick made while a registry source is live
// names one of THAT source's profiles. Routing it through the profile-only
// path resolved the descriptor with a bare name, which the main process
// answers against the primary — the gateway snapped back home and the pick
// looked like it never took.

const ensureGatewayForProfile = vi.fn(async (_profile: string) => undefined)
const ensureGatewayForAgent = vi.fn(async (_connectionId: null | string, _profile: string) => true)
const openGatewayForProfile = vi.fn(async (_profile: string) => undefined)
const activeGatewayConnectionId = vi.fn<() => null | string>(() => null)
const gatewayActivationEpoch = vi.fn(() => 1)
const $gateway = atom<unknown>({ id: 'live-socket' })
const resetStarmapGraph = vi.fn()

vi.mock('@/store/gateway', () => ({
  $gateway,
  activeGatewayConnectionId,
  ensureGatewayForAgent,
  ensureGatewayForProfile,
  gatewayActivationEpoch,
  openGatewayForProfile
}))
vi.mock('@/hermes', () => ({
  getProfiles: vi.fn(async () => ({ profiles: [] })),
  setApiRequestProfile: vi.fn()
}))
vi.mock('@/lib/query-client', () => ({ invalidateProfileScopedQueries: vi.fn() }))
vi.mock('@/store/starmap', () => ({ resetStarmapGraph }))

const {
  $activeGatewayProfile,
  $freshSessionRequest,
  ensureGatewayProfile,
  newSessionInProfile,
  requestFreshSession,
  selectProfile
} = await import('./profile')
const {
  $profileSwitchRestoreToken,
  _resetProfileSwitchBehaviorForTests,
  requestProfileSwitchRestore,
  setProfileSwitchBehavior
} = await import('./profile-switch-behavior')
const { setConnection } = await import('./session')

beforeEach(() => {
  ensureGatewayForProfile.mockClear()
  ensureGatewayForAgent.mockClear()
  ensureGatewayForProfile.mockImplementation(async profile => {
    activeGatewayConnectionId.mockReturnValue(null)
    $activeGatewayProfile.set(profile)
  })
  ensureGatewayForAgent.mockImplementation(async (_connectionId, profile) => {
    $activeGatewayProfile.set(profile)

    return true
  })
  activeGatewayConnectionId.mockReset()
  activeGatewayConnectionId.mockReturnValue(null)
  $gateway.set({ id: 'live-socket' })
  $activeGatewayProfile.set('default')
  setConnection(null)
  _resetProfileSwitchBehaviorForTests()
  // resolveConnectionForAgent is best-effort; without a bridge it resolves
  // null and the previous descriptor stays, which is fine here.
  ;(globalThis as { window?: unknown }).window = {}
})

describe('selectProfile', () => {
  const restoreGeneration = (): number => {
    const generation = $profileSwitchRestoreToken.get()?.generation

    if (generation === undefined) {
      throw new Error('expected an active profile-switch restore generation')
    }

    return generation
  }

  it('activates the pick on the live registry source, not the primary', async () => {
    activeGatewayConnectionId.mockReturnValue('mini')

    selectProfile('researcher')

    await vi.waitFor(() => expect(ensureGatewayForAgent).toHaveBeenCalledWith('mini', 'researcher'))
    expect(ensureGatewayForProfile).not.toHaveBeenCalled()
  })

  it('keeps the legacy profile-only path when the primary is live', async () => {
    activeGatewayConnectionId.mockReturnValue(null)

    selectProfile('ops')

    await vi.waitFor(() => expect(ensureGatewayForProfile).toHaveBeenCalledWith('ops'))
    expect(ensureGatewayForAgent).not.toHaveBeenCalled()
  })

  it('keeps the legacy profile-only path when the explicit local source is live', async () => {
    activeGatewayConnectionId.mockReturnValue('local')

    selectProfile('override-profile')

    await vi.waitFor(() => expect(ensureGatewayForProfile).toHaveBeenCalledWith('override-profile'))
    expect(ensureGatewayForAgent).not.toHaveBeenCalled()
  })

  it('binds a profile-only restore to the remote override that actually activated', async () => {
    activeGatewayConnectionId.mockReturnValue(null)
    ensureGatewayForProfile.mockImplementationOnce(async profile => {
      $activeGatewayProfile.set(profile)
      setConnection({ connectionId: 'remote-override', mode: 'remote', profile } as never)
    })
    setProfileSwitchBehavior('restore_last_session')

    selectProfile('ops')

    await vi.waitFor(() => expect($profileSwitchRestoreToken.get()?.activation?.activationEpoch).toBe(1))
    expect($profileSwitchRestoreToken.get()).toMatchObject({
      activation: {
        descriptorConnectionId: 'remote-override',
        liveGatewayConnectionId: null,
        profile: 'ops'
      },
      requestedConnectionId: null
    })
  })

  it('reverses an owned A -> B restore intent back to A behind the serialized activation', async () => {
    let releaseBeta!: () => void

    setProfileSwitchBehavior('restore_last_session')
    ensureGatewayForProfile.mockImplementationOnce(
      profile =>
        new Promise<undefined>(resolve => {
          releaseBeta = () => {
            $activeGatewayProfile.set(profile)
            resolve(undefined)
          }
        })
    )

    selectProfile('beta')
    await vi.waitFor(() => expect(ensureGatewayForProfile).toHaveBeenCalledWith('beta'))
    const betaGeneration = restoreGeneration()

    selectProfile('default')
    const alphaGeneration = restoreGeneration()

    expect(alphaGeneration).toBeGreaterThan(betaGeneration)
    expect(ensureGatewayForProfile).toHaveBeenCalledTimes(1)

    releaseBeta()

    await vi.waitFor(() => expect(ensureGatewayForProfile).toHaveBeenNthCalledWith(2, 'default'))
    await vi.waitFor(() => expect($activeGatewayProfile.get()).toBe('default'))
    expect($profileSwitchRestoreToken.get()).toMatchObject({
      activation: { profile: 'default' },
      generation: alphaGeneration,
      requestedProfile: 'default'
    })
  })

  it('does not turn a same-profile click into a switch because another activation is in flight', async () => {
    let release!: () => void
    ensureGatewayForProfile.mockImplementationOnce(
      () =>
        new Promise<undefined>(resolve => {
          release = () => resolve(undefined)
        })
    )

    const unrelatedActivation = ensureGatewayProfile('background')
    await vi.waitFor(() => expect(ensureGatewayForProfile).toHaveBeenCalledWith('background'))
    const freshGeneration = $freshSessionRequest.get()

    selectProfile('default')

    expect($freshSessionRequest.get()).toBe(freshGeneration)
    expect($profileSwitchRestoreToken.get()).toBeNull()

    release()
    await unrelatedActivation
    expect(ensureGatewayForProfile).toHaveBeenCalledTimes(1)
  })
})

describe('newSessionInProfile', () => {
  it('opens the new chat on the live registry source', async () => {
    activeGatewayConnectionId.mockReturnValue('mini')

    newSessionInProfile('designer')

    await vi.waitFor(() => expect(ensureGatewayForAgent).toHaveBeenCalledWith('mini', 'designer'))
    expect(ensureGatewayForProfile).not.toHaveBeenCalled()
  })

  it('keeps the legacy profile-only path for a new chat on the explicit local source', async () => {
    activeGatewayConnectionId.mockReturnValue('local')

    newSessionInProfile('override-profile')

    await vi.waitFor(() => expect(ensureGatewayForProfile).toHaveBeenCalledWith('override-profile'))
    expect(ensureGatewayForAgent).not.toHaveBeenCalled()
  })

  it('supersedes a pending restore even when the fresh route and focus do not change', async () => {
    setProfileSwitchBehavior('restore_last_session')
    selectProfile('pending')
    const freshBefore = $freshSessionRequest.get()

    newSessionInProfile('designer')

    expect($profileSwitchRestoreToken.get()).toBeNull()
    expect($freshSessionRequest.get()).toBe(freshBefore + 1)
  })
})

describe('fresh request generations', () => {
  it('does not let an old profile-switch request clear or advance a newer generation', () => {
    const old = requestProfileSwitchRestore('beta', null, $freshSessionRequest.get() + 1)
    const current = requestProfileSwitchRestore('gamma', null, $freshSessionRequest.get() + 1)
    const sequence = $freshSessionRequest.get()

    expect(requestFreshSession(old.generation)).toBe(sequence)
    expect($freshSessionRequest.get()).toBe(sequence)
    expect($profileSwitchRestoreToken.get()?.generation).toBe(current.generation)
  })
})

describe('selectProfile startup preference (#79886)', () => {
  const rememberProfile = vi.fn(async (name: null | string) => ({ profile: name }))

  beforeEach(() => {
    rememberProfile.mockClear()

    const getConnection = vi.fn(async () => ({ mode: 'local' }))

    const getConnectionConfig = vi.fn(async () => ({ mode: 'local' }))

    ;(globalThis as { window?: unknown }).window = {
      hermesDesktop: {
        getConnection,
        getConnectionConfig,
        profile: { remember: rememberProfile }
      }
    }
  })

  it('remembers the selected workspace for the next Desktop launch', async () => {
    activeGatewayConnectionId.mockReturnValue(null)

    selectProfile('tilly')

    await vi.waitFor(() => expect(rememberProfile).toHaveBeenCalledWith('tilly'))
    expect(ensureGatewayForProfile).toHaveBeenCalledWith('tilly')
  })

  it('waits for gateway activation before replacing the startup preference', async () => {
    let resolveGateway!: () => void

    activeGatewayConnectionId.mockReturnValue(null)
    ensureGatewayForProfile.mockImplementationOnce(
      () =>
        new Promise<undefined>(resolve => {
          resolveGateway = () => {
            $activeGatewayProfile.set('tilly')
            resolve(undefined)
          }
        })
    )

    selectProfile('tilly')
    await vi.waitFor(() => expect(ensureGatewayForProfile).toHaveBeenCalledWith('tilly'))
    expect(rememberProfile).not.toHaveBeenCalled()

    resolveGateway()

    await vi.waitFor(() => expect(rememberProfile).toHaveBeenCalledWith('tilly'))
  })

  it('does not replace the startup preference for a registry-source pick', async () => {
    activeGatewayConnectionId.mockReturnValue('mini')

    selectProfile('researcher')

    await vi.waitFor(() => expect(ensureGatewayForAgent).toHaveBeenCalledWith('mini', 'researcher'))
    expect(rememberProfile).not.toHaveBeenCalled()
  })

  it('remembers an already-active local profile after returning from All Profiles', async () => {
    activeGatewayConnectionId.mockReturnValue(null)
    $activeGatewayProfile.set('tilly')

    selectProfile('tilly')

    await vi.waitFor(() => expect(rememberProfile).toHaveBeenCalledWith('tilly'))
  })

  it('keeps local startup persistence when the backend descriptor lookup fails', async () => {
    activeGatewayConnectionId.mockReturnValue(null)

    const getConnection = vi.fn(async () => {
      throw new Error('descriptor unavailable')
    })

    const getConnectionConfig = vi.fn(async () => ({ mode: 'local' }))

    ;(globalThis as { window?: unknown }).window = {
      hermesDesktop: {
        getConnection,
        getConnectionConfig,
        profile: { remember: rememberProfile }
      }
    }

    selectProfile('tilly')

    await vi.waitFor(() => expect(ensureGatewayForProfile).toHaveBeenCalledWith('tilly'))
    await vi.waitFor(() => expect(rememberProfile).toHaveBeenCalledWith('tilly'))
  })

  it('does not replace the local startup preference for a profile SSH override', async () => {
    activeGatewayConnectionId.mockReturnValue(null)

    const getConnection = vi.fn(async () => ({ mode: 'remote', remoteKind: 'ssh' }))

    const getConnectionConfig = vi.fn(async () => ({ mode: 'ssh' }))

    ;(globalThis as { window?: unknown }).window = {
      hermesDesktop: {
        getConnection,
        getConnectionConfig,
        profile: { remember: rememberProfile }
      }
    }

    selectProfile('macmini-hermes')

    await vi.waitFor(() => expect(ensureGatewayForProfile).toHaveBeenCalledWith('macmini-hermes'))
    await vi.waitFor(() => expect(getConnection).toHaveBeenCalledWith('macmini-hermes'))
    await new Promise(resolve => setTimeout(resolve, 0))

    expect(rememberProfile).not.toHaveBeenCalled()
  })
})
