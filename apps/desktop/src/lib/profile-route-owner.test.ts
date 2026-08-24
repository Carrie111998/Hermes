import { describe, expect, it, vi } from 'vitest'

import { buildProfileRouteOwner, evaluateProfileReadiness, profileNamesForConnection } from './profile-route-owner'
import type { RuntimeReadinessOptions, RuntimeReadinessRequester, RuntimeReadinessResult } from './runtime-readiness'

describe('remote Bot creation routing', () => {
  const connections = [
    { id: 'local', kind: 'local', label: 'This device' },
    { id: 'mini', kind: 'ssh', label: 'Mac Mini' }
  ]

  it('captures the selected connection and created profile as one immutable owner', () => {
    const owner = buildProfileRouteOwner({
      activeConnectionId: 'local',
      connections,
      name: 'omar',
      targetConnection: 'mini'
    })

    expect(owner).toMatchObject({
      connectionId: 'mini',
      connectionLabel: 'Mac Mini',
      name: 'omar',
      remoteSource: true,
      sourceScoped: true
    })
    expect(owner.route).toEqual({
      connectionId: 'mini',
      mode: 'remote',
      profile: 'omar',
      targetProfile: 'omar'
    })
    expect(Object.isFrozen(owner.route)).toBe(true)
  })

  it('offers fresh/default plus clone profiles from only the selected connection', () => {
    const roster = [
      { connectionId: 'local', name: 'default', remoteSource: false },
      { connectionId: 'local', name: 'writer', remoteSource: false },
      { connectionId: 'mini', name: 'default', remoteSource: true },
      { connectionId: 'mini', name: 'omar', remoteSource: true },
      { connectionId: 'work', name: 'omar', remoteSource: true }
    ]

    expect(profileNamesForConnection(roster, 'mini', true)).toEqual(['default', 'omar'])
    expect(profileNamesForConnection(roster, 'local', false)).toEqual(['default', 'writer'])
  })

  it('checks readiness through the created profile route and preserves a not-ready result', async () => {
    const owner = buildProfileRouteOwner({
      activeConnectionId: 'local',
      connections,
      name: 'omar',
      targetConnection: 'mini'
    })

    const request = vi.fn(async (_owner, method: string, params?: Record<string, unknown>) => ({ method, params }))

    const evaluate = vi.fn(
      async (
        requester: RuntimeReadinessRequester,
        options: RuntimeReadinessOptions = {}
      ): Promise<RuntimeReadinessResult> => {
        await requester('setup.status')
        await requester('setup.runtime_check', { provider: options.requestedProvider })

        return {
          checksDisagree: false,
          ready: false,
          reason: options.defaultReason ?? null,
          source: 'runtime_check'
        }
      }
    )

    const result = await evaluateProfileReadiness({
      evaluate,
      owner,
      requestedProvider: 'nous',
      request
    })

    expect(result).toMatchObject({ ready: false, source: 'runtime_check' })
    expect(result.reason).toContain('Mac Mini')
    expect(request).toHaveBeenNthCalledWith(1, owner, 'setup.status', undefined)
    expect(request).toHaveBeenNthCalledWith(2, owner, 'setup.runtime_check', { provider: 'nous' })
  })
})
