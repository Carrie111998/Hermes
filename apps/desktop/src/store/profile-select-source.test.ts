import { LOCAL_CONNECTION_ID } from '@hermes/shared'
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
const openGatewayForAgent = vi.fn(async (_connectionId: null | string, _profile: string) => undefined)
const openGatewayForProfile = vi.fn(async (_profile: string) => undefined)
const activeGatewayConnectionId = vi.fn<() => null | string>(() => null)
const $gateway = atom<unknown>({ id: 'live-socket' })
const resetStarmapGraph = vi.fn()

vi.mock('@/store/gateway', () => ({
  $gateway,
  activeGatewayConnectionId,
  ensureGatewayForAgent,
  ensureGatewayForProfile,
  openGatewayForAgent,
  openGatewayForProfile
}))
vi.mock('@/hermes', () => ({
  getProfiles: vi.fn(async () => ({ profiles: [] })),
  setApiRequestProfile: vi.fn()
}))
vi.mock('@/lib/query-client', () => ({ invalidateProfileScopedQueries: vi.fn() }))
vi.mock('@/store/starmap', () => ({ resetStarmapGraph }))

const { $activeGatewayProfile, $newChatRoute, newSessionInProfile, prewarmProfileBackend, selectProfile } =
  await import('./profile')

beforeEach(() => {
  ensureGatewayForProfile.mockClear()
  ensureGatewayForAgent.mockClear()
  openGatewayForAgent.mockClear()
  openGatewayForProfile.mockClear()
  activeGatewayConnectionId.mockReset()
  activeGatewayConnectionId.mockReturnValue(null)
  $gateway.set({ id: 'live-socket' })
  $activeGatewayProfile.set('default')
  // resolveConnectionForAgent is best-effort; without a bridge it resolves
  // null and the previous descriptor stays, which is fine here.
  ;(globalThis as { window?: unknown }).window = {}
})

describe('selectProfile', () => {
  it('activates the pick on the live registry source, not the primary', async () => {
    activeGatewayConnectionId.mockReturnValue('mini')

    selectProfile('researcher')

    await vi.waitFor(() => expect(ensureGatewayForAgent).toHaveBeenCalledWith('mini', 'researcher'))
    expect(ensureGatewayForProfile).not.toHaveBeenCalled()
    expect($newChatRoute.get()).toEqual({ connectionId: 'mini', profile: 'researcher' })
  })

  it('keeps the legacy profile-only path when the primary is live', async () => {
    activeGatewayConnectionId.mockReturnValue(null)

    selectProfile('ops')

    await vi.waitFor(() => expect(ensureGatewayForProfile).toHaveBeenCalledWith('ops'))
    expect(ensureGatewayForAgent).not.toHaveBeenCalled()
    expect($newChatRoute.get()).toBeNull()
  })

  it('keeps the legacy profile-only path for the explicit local source', async () => {
    activeGatewayConnectionId.mockReturnValue(LOCAL_CONNECTION_ID)

    selectProfile('ops')

    await vi.waitFor(() => expect(ensureGatewayForProfile).toHaveBeenCalledWith('ops'))
    expect(ensureGatewayForAgent).not.toHaveBeenCalled()
    expect($newChatRoute.get()).toBeNull()
  })
})

describe('newSessionInProfile', () => {
  it('opens the new chat on the live registry source', async () => {
    activeGatewayConnectionId.mockReturnValue('mini')

    newSessionInProfile('designer')

    await vi.waitFor(() => expect(ensureGatewayForAgent).toHaveBeenCalledWith('mini', 'designer'))
    expect(ensureGatewayForProfile).not.toHaveBeenCalled()
    expect($newChatRoute.get()).toEqual({ connectionId: 'mini', profile: 'designer' })
  })

  it('keeps the legacy profile-only path for a primary new chat', async () => {
    newSessionInProfile('operator')

    await vi.waitFor(() => expect(ensureGatewayForProfile).toHaveBeenCalledWith('operator'))
    expect(ensureGatewayForAgent).not.toHaveBeenCalled()
    expect($newChatRoute.get()).toBeNull()
  })

  it('keeps the legacy profile-only path for an explicit local new chat', async () => {
    activeGatewayConnectionId.mockReturnValue(LOCAL_CONNECTION_ID)

    newSessionInProfile('operator')

    await vi.waitFor(() => expect(ensureGatewayForProfile).toHaveBeenCalledWith('operator'))
    expect(ensureGatewayForAgent).not.toHaveBeenCalled()
    expect($newChatRoute.get()).toBeNull()
  })
})

describe('prewarmProfileBackend', () => {
  it('prewarms a profile on the live registry source, not the primary', async () => {
    activeGatewayConnectionId.mockReturnValue('mini')

    prewarmProfileBackend('reviewer')

    await vi.waitFor(() => expect(openGatewayForAgent).toHaveBeenCalledWith('mini', 'reviewer'))
    expect(openGatewayForProfile).not.toHaveBeenCalled()
  })

  it('keeps the profile-only prewarm path when the primary is live', async () => {
    activeGatewayConnectionId.mockReturnValue(null)

    prewarmProfileBackend('operator')

    await vi.waitFor(() => expect(openGatewayForProfile).toHaveBeenCalledWith('operator'))
    expect(openGatewayForAgent).not.toHaveBeenCalled()
  })

  it('throttles prewarm independently for the same profile on different sources', () => {
    activeGatewayConnectionId.mockReturnValue('mini')
    prewarmProfileBackend('shared-profile')

    activeGatewayConnectionId.mockReturnValue('studio')
    prewarmProfileBackend('shared-profile')

    expect(openGatewayForAgent).toHaveBeenNthCalledWith(1, 'mini', 'shared-profile')
    expect(openGatewayForAgent).toHaveBeenNthCalledWith(2, 'studio', 'shared-profile')
  })
})
