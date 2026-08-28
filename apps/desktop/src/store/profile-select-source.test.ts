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
const $gateway = atom<unknown>({ id: 'live-socket' })
const resetStarmapGraph = vi.fn()

vi.mock('@/store/gateway', () => ({
  $gateway,
  activeGatewayConnectionId,
  ensureGatewayForAgent,
  ensureGatewayForProfile,
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
  $freshSessionIntent,
  $showAllProfiles,
  newSessionInProfile,
  selectProfile,
  setShowAllProfiles
} = await import('./profile')
const { $profileConversationRestore, _resetProfileConversationRestoreForTests } =
  await import('./profile-conversation-restore')

beforeEach(() => {
  ensureGatewayForProfile.mockClear()
  ensureGatewayForAgent.mockClear()
  activeGatewayConnectionId.mockReset()
  activeGatewayConnectionId.mockReturnValue(null)
  $gateway.set({ id: 'live-socket' })
  $activeGatewayProfile.set('default')
  $showAllProfiles.set(false)
  _resetProfileConversationRestoreForTests()
  // resolveConnectionForAgent is best-effort; without a bridge it resolves
  // null and the previous descriptor stays, which is fine here. Preserve the
  // jsdom Window itself because notifications rely on its timer APIs.
  ;(window as unknown as { hermesDesktop?: unknown }).hermesDesktop = undefined
})

describe('selectProfile', () => {
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

  it('begins before isolation and commits only after activation succeeds', async () => {
    let resolveActivation!: () => void

    ensureGatewayForProfile.mockImplementationOnce(
      () =>
        new Promise<undefined>(resolve => {
          resolveActivation = () => resolve(undefined)
        })
    )

    selectProfile('research')

    const activating = $profileConversationRestore.get()
    expect(activating).toMatchObject({ phase: 'activating', target: { connectionId: null, profile: 'research' } })
    expect($freshSessionIntent.get()).toMatchObject({
      cause: 'profile-switch',
      persistence: 'automatic',
      restoreSequence: activating?.sequence
    })

    resolveActivation()
    await vi.waitFor(() => expect($profileConversationRestore.get()?.phase).toBe('committed'))
  })

  it('cancels the matching restore when activation fails', async () => {
    ensureGatewayForProfile.mockRejectedValueOnce(new Error('offline'))

    selectProfile('research')

    await vi.waitFor(() => expect($profileConversationRestore.get()).toBeNull())
  })

  it('keeps only the latest restore across rapid selections', async () => {
    let resolveFirst!: () => void

    ensureGatewayForProfile.mockImplementationOnce(
      () =>
        new Promise<undefined>(resolve => {
          resolveFirst = () => resolve(undefined)
        })
    )

    selectProfile('research')
    const first = $profileConversationRestore.get()?.sequence
    selectProfile('ops')
    resolveFirst()

    await vi.waitFor(() =>
      expect($profileConversationRestore.get()).toMatchObject({ phase: 'committed', target: { profile: 'ops' } })
    )

    expect($profileConversationRestore.get()?.sequence).not.toBe(first)
    expect($profileConversationRestore.get()?.target.profile).toBe('ops')
  })

  it('restores on concrete re-entry from All Profiles but not a same-profile retap', async () => {
    $activeGatewayProfile.set('default')

    selectProfile('default')
    await Promise.resolve()
    expect($profileConversationRestore.get()).toBeNull()

    setShowAllProfiles(true)
    selectProfile('default')
    await vi.waitFor(() => expect($profileConversationRestore.get()?.phase).toBe('committed'))
  })
})

describe('newSessionInProfile', () => {
  it('opens the new chat on the live registry source', async () => {
    activeGatewayConnectionId.mockReturnValue('mini')

    newSessionInProfile('designer')

    await vi.waitFor(() => expect(ensureGatewayForAgent).toHaveBeenCalledWith('mini', 'designer'))
    expect(ensureGatewayForProfile).not.toHaveBeenCalled()
    expect($profileConversationRestore.get()).toBeNull()
    expect($freshSessionIntent.get()).toMatchObject({
      cause: 'new-chat-in-profile',
      persistence: 'explicit'
    })
  })

  it('keeps the legacy profile-only path for a new chat on the explicit local source', async () => {
    activeGatewayConnectionId.mockReturnValue('local')

    newSessionInProfile('override-profile')

    await vi.waitFor(() => expect(ensureGatewayForProfile).toHaveBeenCalledWith('override-profile'))
    expect(ensureGatewayForAgent).not.toHaveBeenCalled()
  })
})

describe('selectProfile startup preference (#79886)', () => {
  const rememberProfile = vi.fn(async (name: null | string) => ({ profile: name }))

  beforeEach(() => {
    rememberProfile.mockClear()

    const getConnection = vi.fn(async () => ({ mode: 'local' }))

    const getConnectionConfig = vi.fn(async () => ({ mode: 'local' }))

    ;(window as unknown as { hermesDesktop?: unknown }).hermesDesktop = {
      getConnection,
      getConnectionConfig,
      profile: { remember: rememberProfile }
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
          resolveGateway = () => resolve(undefined)
        })
    )

    selectProfile('tilly')
    await vi.waitFor(() => expect(ensureGatewayForProfile).toHaveBeenCalledWith('tilly'))
    expect(rememberProfile).not.toHaveBeenCalled()

    resolveGateway()

    await vi.waitFor(() => expect(rememberProfile).toHaveBeenCalledWith('tilly'))
  })

  it('keeps a landed restore committed when startup-profile persistence fails', async () => {
    activeGatewayConnectionId.mockReturnValue(null)
    rememberProfile.mockRejectedValueOnce(new Error('read-only userData'))

    selectProfile('tilly')

    await vi.waitFor(() => expect(rememberProfile).toHaveBeenCalledWith('tilly'))
    await vi.waitFor(() => expect($profileConversationRestore.get()?.phase).toBe('committed'))
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

    ;(window as unknown as { hermesDesktop?: unknown }).hermesDesktop = {
      getConnection,
      getConnectionConfig,
      profile: { remember: rememberProfile }
    }

    selectProfile('tilly')

    await vi.waitFor(() => expect(ensureGatewayForProfile).toHaveBeenCalledWith('tilly'))
    await vi.waitFor(() => expect(rememberProfile).toHaveBeenCalledWith('tilly'))
  })

  it('does not replace the local startup preference for a profile SSH override', async () => {
    activeGatewayConnectionId.mockReturnValue(null)

    const getConnection = vi.fn(async () => ({ mode: 'remote', remoteKind: 'ssh' }))

    const getConnectionConfig = vi.fn(async () => ({ mode: 'ssh' }))

    ;(window as unknown as { hermesDesktop?: unknown }).hermesDesktop = {
      getConnection,
      getConnectionConfig,
      profile: { remember: rememberProfile }
    }

    selectProfile('macmini-hermes')

    await vi.waitFor(() => expect(ensureGatewayForProfile).toHaveBeenCalledWith('macmini-hermes'))
    await vi.waitFor(() => expect(getConnection).toHaveBeenCalledWith('macmini-hermes'))
    await new Promise(resolve => setTimeout(resolve, 0))

    expect(rememberProfile).not.toHaveBeenCalled()
  })
})
