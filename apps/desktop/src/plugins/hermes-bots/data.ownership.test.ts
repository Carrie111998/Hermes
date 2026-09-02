import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { RosterRow } from './types'

const { hostMock } = vi.hoisted(() => ({
  hostMock: {
    request: vi.fn(),
    requestProfile: vi.fn(),
    state: { connectionId: { get: () => 'local' }, profile: { get: () => 'default' } }
  }
}))

vi.mock('@hermes/plugin-sdk', async () => {
  const { atom } = await import('nanostores')

  return {
    atom,
    host: hostMock,
    queryClient: {},
    useQuery: vi.fn(),
    useValue: vi.fn()
  }
})

vi.mock('./shared', () => ({ getPluginCtx: () => null, ID: 'hermes-bots' }))

const { reconcileBotModeOwnership, resetBotModeOwnershipReconciliationForTests } = await import('./data')

const remote = (connectionId: string): RosterRow =>
  ({ connectionId, name: 'default', remoteSource: true, sourceScoped: true }) as RosterRow

beforeEach(() => {
  vi.clearAllMocks()
  resetBotModeOwnershipReconciliationForTests()
})

describe('Bot Mode ownership reconciliation', () => {
  it('repairs an absent marker once and requires readback', async () => {
    hostMock.requestProfile
      .mockResolvedValueOnce({ profiles: [{ name: 'default', ui_meta: { another: { kept: true } } }] })
      .mockResolvedValueOnce({ applied: { ui_meta: true } })
      .mockResolvedValueOnce({ profiles: [{ name: 'default', ui_meta: { 'hermes-bots': {} } }] })

    await reconcileBotModeOwnership([remote('spark01')], 10_000)
    await reconcileBotModeOwnership([remote('spark01')], 10_001)

    expect(hostMock.requestProfile).toHaveBeenCalledTimes(3)
    expect(hostMock.requestProfile).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({ connectionId: 'spark01' }),
      'profiles.configure',
      { name: 'default', ui_meta: { 'hermes-bots': {} } }
    )
  })

  it('does not write when any profile already owns Bot Mode', async () => {
    hostMock.requestProfile.mockResolvedValue({
      profiles: [{ name: 'auditor', ui_meta: { 'hermes-bots': { title: 'Auditor' } } }]
    })

    await reconcileBotModeOwnership([remote('spark01')], 20_000)

    expect(hostMock.requestProfile).toHaveBeenCalledTimes(1)
    expect(hostMock.requestProfile).toHaveBeenCalledWith(expect.anything(), 'profiles.list', {})
  })

  it('retries rejected or unconfirmed repairs and keeps gateways independent', async () => {
    hostMock.requestProfile.mockImplementation(async (route, method) => {
      if (method === 'profiles.list') {
        return { profiles: [{ name: 'default', ui_meta: {} }] }
      }

      return { applied: { ui_meta: route.connectionId === 'spark02' } }
    })

    await reconcileBotModeOwnership([remote('spark01'), remote('spark02')], 30_000)
    await reconcileBotModeOwnership([remote('spark01'), remote('spark02')], 30_001)

    const writes = hostMock.requestProfile.mock.calls.filter(([, method]) => method === 'profiles.configure')
    expect(writes.map(([route]) => route.connectionId)).toEqual(['spark01', 'spark02', 'spark01', 'spark02'])
  })
})
