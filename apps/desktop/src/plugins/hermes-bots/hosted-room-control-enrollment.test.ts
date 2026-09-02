import { createHash } from 'node:crypto'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { classifyHostedRoomCapability } from './hosted-room-client'
import { createHostedRoomControlEnrollment, type HostedRoomServerState } from './hosted-room-control-enrollment'
import type { GroupMember, ProfileRoute } from './types'

const { host } = vi.hoisted(() => ({
  host: { request: vi.fn(), requestProfile: vi.fn() }
}))

vi.mock('@hermes/plugin-sdk', () => ({ host }))

const homeRoute: ProfileRoute = {
  connectionId: 'home',
  mode: 'remote',
  profile: 'default',
  targetProfile: 'default'
}

const peerRoute: ProfileRoute = { ...homeRoute, connectionId: 'peer' }
const routes = { home: homeRoute, peer: peerRoute }

const capabilities = Object.fromEntries(
  Object.keys(routes).map(connectionId => [
    connectionId,
    classifyHostedRoomCapability(
      {
        authority_gateway_id: `install:${connectionId}`,
        driver: true,
        features: ['reciprocal_room_control'],
        persistent_process: true
      },
      { connectionId }
    )
  ])
)

const invitation = {
  authority_epoch: 1,
  authority_gateway_id: 'install:home',
  control_token: 'test-control-token',
  expires_at: 2_000_000_000,
  home_url: 'https://home.example.test:19445',
  member_count: 2,
  room_name: 'Release'
}

const member: GroupMember = {
  name: 'builder-alias',
  sourceScoped: true,
  route: { ...peerRoute, profile: 'builder-alias', targetProfile: 'builder' }
}

function room(overrides: Partial<HostedRoomServerState> = {}): HostedRoomServerState {
  return {
    room_id: 'room-1',
    authority_epoch: 1,
    members: [
      {
        member_id: 'member-1',
        profile: 'builder',
        target: { kind: 'peer', installation_id: 'install:peer' }
      }
    ],
    ...overrides
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void

  const promise = new Promise<T>((yes, no) => {
    resolve = yes
    reject = no
  })

  return { promise, resolve, reject }
}

type RequestConnection = Parameters<typeof createHostedRoomControlEnrollment>[0]

function setup(
  handler: RequestConnection = async (_route, method) =>
    method === 'groups.control.invite' ? invitation : { registered: true }
) {
  const request = vi.fn(handler)
  const enrollment = createHostedRoomControlEnrollment(request)
  enrollment.reset(true)

  return { enrollment, request }
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(1_000_000)
  host.request.mockReset().mockRejectedValue(new Error('Unscoped gateway must not be used'))
  host.requestProfile.mockReset().mockResolvedValue({ registered: true })
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('reciprocal room control reconciliation', () => {
  it('serializes rooms, coalesces pending work, and caches only completed enrollment', async () => {
    const blocked = deferred<unknown>()

    const { enrollment, request } = setup(async (_route, method, params) => {
      if (method === 'groups.control.invite') {
        return params.room_id === 'room-1' ? blocked.promise : invitation
      }

      return { registered: true }
    })

    const first = enrollment.schedule(room(), homeRoute, routes, capabilities)
    const duplicate = enrollment.schedule(room(), homeRoute, routes, capabilities)
    const second = enrollment.schedule(room({ room_id: 'room-2' }), homeRoute, routes, capabilities)
    expect(duplicate).toBe(first)
    await vi.waitFor(() => expect(request).toHaveBeenCalledTimes(1))
    blocked.resolve(invitation)
    await Promise.all([first, second])

    expect(request.mock.calls.map(([, method, params]) => [method, params.room_id])).toEqual([
      ['groups.control.invite', 'room-1'],
      ['groups.control.register', 'room-1'],
      ['groups.control.invite', 'room-2'],
      ['groups.control.register', 'room-2']
    ])
    expect(request.mock.calls[0]).toEqual([
      homeRoute,
      'groups.control.invite',
      {
        room_id: 'room-1',
        member_id: 'member-1',
        caller_install_id: 'install:peer',
        request_id: `room-control:${createHash('sha256').update('room-control-v1\0room-1\0member-1').digest('hex')}`
      }
    ])
    expect(request.mock.calls[1]).toEqual([
      peerRoute,
      'groups.control.register',
      { ...invitation, room_id: 'room-1', member_id: 'member-1', profile: 'builder' }
    ])
    await enrollment.schedule(room(), homeRoute, routes, capabilities)
    expect(request).toHaveBeenCalledTimes(4)
  })

  it('scopes the success cache by room, epoch, member and authority, not connection alias', async () => {
    const { enrollment, request } = setup()
    await enrollment.schedule(room(), homeRoute, routes, capabilities)
    await enrollment.schedule(
      room(),
      homeRoute,
      { ...routes, alias: { ...peerRoute, connectionId: 'alias' } },
      { home: capabilities.home, alias: capabilities.peer }
    )
    expect(request).toHaveBeenCalledTimes(2)

    await enrollment.schedule(room({ authority_epoch: 2 }), homeRoute, routes, capabilities)
    await enrollment.schedule(
      room({ members: [{ member_id: 'member-2', target: { kind: 'peer', peer_id: 'install:peer' } }] }),
      homeRoute,
      routes,
      capabilities
    )
    await enrollment.schedule(
      room({ members: [{ member_id: 'member-1', target: { kind: 'peer', peer_id: 'install:other' } }] }),
      homeRoute,
      routes,
      { ...capabilities, peer: { ...capabilities.peer, authorityId: 'install:other' } }
    )
    const invites = request.mock.calls.filter(([, method]) => method === 'groups.control.invite')
    expect(invites).toHaveLength(4)
    expect(invites[1][2].request_id).toBe(invites[0][2].request_id)
    expect(invites[2][2].request_id).not.toBe(invites[0][2].request_id)
    expect(invites[3][2].caller_install_id).toBe('install:other')
    expect(request.mock.calls[5][2].profile).toBe('member-2')
  })

  it.each([0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1, 'invalid'])(
    'rejects an invalid authority epoch (%s) without issuing controls',
    async authority_epoch => {
      const { enrollment, request } = setup()
      await enrollment.schedule(room({ authority_epoch }), homeRoute, routes, capabilities)
      expect(request).not.toHaveBeenCalled()
    }
  )

  it('requires room identity, peer identity, routes and reciprocal capability on both ends', async () => {
    const { enrollment, request } = setup()
    await enrollment.schedule(room({ room_id: '' }), homeRoute, routes, capabilities)
    await enrollment.schedule(room(), homeRoute, routes, {
      ...capabilities,
      home: { ...capabilities.home, reciprocalControl: false }
    })
    await enrollment.schedule(room(), homeRoute, routes, {
      ...capabilities,
      peer: { ...capabilities.peer, reciprocalControl: false }
    })
    await enrollment.schedule(room(), homeRoute, { home: homeRoute }, capabilities)
    await enrollment.schedule(room(), homeRoute, routes, { home: capabilities.home })
    await enrollment.schedule(
      room({
        members: [
          null,
          [],
          { member_id: 'local', target: { kind: 'local' } },
          { member_id: '', target: { kind: 'peer', installation_id: 'install:peer' } },
          { member_id: 'no-authority', target: { kind: 'peer' } }
        ]
      }),
      homeRoute,
      routes,
      capabilities
    )
    expect(request).not.toHaveBeenCalled()
  })

  it.each(['invite', 'register', 'incomplete'])(
    'backs off %s failures for 30 seconds, then retries with the same request identity',
    async failure => {
      let failing = true

      const { enrollment, request } = setup(async (_route, method) => {
        if (failing && method === `groups.control.${failure}`) {
          throw new Error('Unavailable')
        }

        if (method === 'groups.control.invite') {
          return failing && failure === 'incomplete' ? { ...invitation, control_token: '' } : invitation
        }

        return { registered: true }
      })

      await enrollment.schedule(room(), homeRoute, routes, capabilities)
      const failedCalls = request.mock.calls.length
      const requestId = request.mock.calls[0][2].request_id
      failing = false
      await enrollment.schedule(room(), homeRoute, routes, capabilities)
      vi.setSystemTime(1_029_999)
      await enrollment.schedule(room(), homeRoute, routes, capabilities)
      expect(request).toHaveBeenCalledTimes(failedCalls)
      vi.setSystemTime(1_030_000)
      await enrollment.schedule(room(), homeRoute, routes, capabilities)
      expect(request).toHaveBeenCalledTimes(failedCalls + 2)
      expect(request.mock.calls[failedCalls][2].request_id).toBe(requestId)
      await enrollment.schedule(room(), homeRoute, routes, capabilities)
      expect(request).toHaveBeenCalledTimes(failedCalls + 2)
    }
  )

  it('continues to the next member after a failed invite', async () => {
    const { enrollment, request } = setup(async (_route, method, params) => {
      if (params.member_id === 'unavailable') {
        throw new Error('Unavailable')
      }

      return method === 'groups.control.invite' ? invitation : { registered: true }
    })

    const members = ['unavailable', 'ready'].map(member_id => ({
      member_id,
      target: { kind: 'peer', installation_id: 'install:peer' }
    }))

    await enrollment.schedule(room({ members }), homeRoute, routes, capabilities)
    expect(request.mock.calls.map(([, method, params]) => [method, params.member_id])).toEqual([
      ['groups.control.invite', 'unavailable'],
      ['groups.control.invite', 'ready'],
      ['groups.control.register', 'ready']
    ])
  })

  it.each([
    ['invite', 'resolve'],
    ['invite', 'reject'],
    ['register', 'resolve'],
    ['register', 'reject']
  ])('fences a stale %s %s from a restarted pending enrollment', async (stage, outcome) => {
    const old = deferred<unknown>()
    const current = deferred<unknown>()
    let attempts = 0

    const { enrollment, request } = setup(async (_route, method) => {
      if (method === `groups.control.${stage}`) {
        attempts += 1

        return attempts === 1 ? old.promise : current.promise
      }

      return method === 'groups.control.invite' ? invitation : { registered: true }
    })

    const oldWork = enrollment.schedule(room(), homeRoute, routes, capabilities)
    await vi.waitFor(() => expect(attempts).toBe(1))
    enrollment.reset(false)
    await enrollment.schedule(room(), homeRoute, routes, capabilities)
    expect(attempts).toBe(1)
    enrollment.reset(true)
    const currentWork = enrollment.schedule(room(), homeRoute, routes, capabilities)
    await vi.waitFor(() => expect(attempts).toBe(2))

    if (outcome === 'reject') {
      old.reject(new Error('Stale failure'))
    } else {
      old.resolve(invitation)
    }

    await oldWork
    expect(enrollment.schedule(room(), homeRoute, routes, capabilities)).toBe(currentWork)
    current.resolve(invitation)
    await currentWork
    await enrollment.schedule(room(), homeRoute, routes, capabilities)
    expect(attempts).toBe(2)
    expect(request.mock.calls.filter(([, method]) => method === 'groups.control.register')).toHaveLength(
      stage === 'invite' ? 1 : 2
    )
  })

  it('detaches a blocked old queue and skips its queued rooms after reset', async () => {
    const blocked = deferred<unknown>()

    const { enrollment, request } = setup(async (_route, method, params) => {
      if (method === 'groups.control.invite') {
        return params.room_id === 'blocked' ? blocked.promise : invitation
      }

      return { registered: true }
    })

    const oldWork = enrollment.schedule(room({ room_id: 'blocked' }), homeRoute, routes, capabilities)
    const queued = enrollment.schedule(room({ room_id: 'queued' }), homeRoute, routes, capabilities)
    await vi.waitFor(() => expect(request).toHaveBeenCalledTimes(1))
    enrollment.reset(true)
    await enrollment.schedule(room({ room_id: 'current' }), homeRoute, routes, capabilities)
    blocked.resolve(invitation)
    await Promise.all([oldWork, queued])
    expect(request.mock.calls.map(([, method, params]) => [method, params.room_id])).toEqual([
      ['groups.control.invite', 'blocked'],
      ['groups.control.invite', 'current'],
      ['groups.control.register', 'current']
    ])
  })

  it('clears success and backoff caches on a fresh lifecycle', async () => {
    let failing = false

    const { enrollment, request } = setup(async (_route, method) => {
      if (failing) {
        throw new Error('Unavailable')
      }

      return method === 'groups.control.invite' ? invitation : { registered: true }
    })

    await enrollment.schedule(room(), homeRoute, routes, capabilities)
    enrollment.reset(true)
    failing = true
    await enrollment.schedule(room(), homeRoute, routes, capabilities)
    failing = false
    enrollment.reset(true)
    await enrollment.schedule(room(), homeRoute, routes, capabilities)
    expect(request).toHaveBeenCalledTimes(5)
  })
})

describe('creation-time reciprocal room controls', () => {
  it('uses the same invitation identity and the existing bot-scoped registration door', async () => {
    const { enrollment, request } = setup()
    const controls = enrollment.forCreation(homeRoute, 'room-1', capabilities)
    controls.add('member-1', member, 'peer', 'install:peer', 'builder-alias')
    await controls.enroll('member-1')
    expect(host.requestProfile).toHaveBeenCalledWith(member.route, 'groups.control.register', {
      ...invitation,
      room_id: 'room-1',
      member_id: 'member-1',
      profile: 'builder'
    })
    expect(host.request).not.toHaveBeenCalled()
    await enrollment.schedule(room(), homeRoute, routes, capabilities)
    const invites = request.mock.calls.filter(([, method]) => method === 'groups.control.invite')
    expect(invites).toHaveLength(2)
    expect(invites[1][2]).toEqual(invites[0][2])
  })

  it.each(['home', 'peer'])('skips enrollment when %s lacks reciprocal control', async connectionId => {
    const { enrollment, request } = setup()

    const controls = enrollment.forCreation(homeRoute, 'room-1', {
      ...capabilities,
      [connectionId]: { ...capabilities[connectionId], reciprocalControl: false }
    })

    controls.add('member-1', member, 'peer', 'install:peer', 'builder')
    await controls.enroll('member-1')
    expect(request).not.toHaveBeenCalled()
    expect(host.requestProfile).not.toHaveBeenCalled()
  })

  it.each(Object.keys(invitation))('rejects an invitation missing %s before registering', async field => {
    const { enrollment } = setup(async () => ({ ...invitation, [field]: undefined }))
    const controls = enrollment.forCreation(homeRoute, 'room-1', capabilities)
    controls.add('member-1', member, 'peer', 'install:peer', 'builder')
    await expect(controls.enroll('member-1')).rejects.toThrow(
      'One selected Bot could not prepare remote Group Chat control.'
    )
    expect(host.requestProfile).not.toHaveBeenCalled()
  })

  it('propagates registration failure and revokes every prepared member with all-settled cleanup', async () => {
    const { enrollment } = setup()
    const controls = enrollment.forCreation(homeRoute, 'room-1', capabilities)
    controls.add('member-1', member, 'peer', 'install:peer', 'builder')
    controls.add('member-2', member, 'peer', 'install:peer', 'builder')
    host.requestProfile.mockRejectedValue(new Error('Peer unavailable'))
    await expect(controls.enroll('member-1')).rejects.toThrow('Peer unavailable')
    await expect(controls.revoke()).resolves.toBeUndefined()
    expect(host.requestProfile.mock.calls.filter(([, method]) => method === 'groups.control.revoke')).toEqual([
      [member.route, 'groups.control.revoke', { room_id: 'room-1', member_id: 'member-1' }],
      [member.route, 'groups.control.revoke', { room_id: 'room-1', member_id: 'member-2' }]
    ])
  })

  it('keeps caller-awaited creation independent of background stop and reset', async () => {
    const blocked = deferred<unknown>()
    const { enrollment, request } = setup(async () => blocked.promise)
    const controls = enrollment.forCreation(homeRoute, 'room-1', capabilities)
    controls.add('member-1', member, 'peer', 'install:peer', 'builder')
    const creating = controls.enroll('member-1')
    await vi.waitFor(() => expect(request).toHaveBeenCalledTimes(1))
    enrollment.reset(false)
    blocked.resolve(invitation)
    await creating
    expect(host.requestProfile).toHaveBeenCalledTimes(1)
    await controls.revoke()
    expect(host.requestProfile).toHaveBeenCalledTimes(2)
  })
})
