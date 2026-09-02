import { afterEach, describe, expect, it, vi } from 'vitest'

import type * as groupChat from './group-chat'
import type * as groupRounds from './group-rounds'
import { pluginSdkMock, scriptedStorage } from './group-test-utils'
import type * as groupTurns from './group-turns'
import type * as hostedRuntime from './hosted-room-runtime'
import type { GroupChat, GroupMember } from './types'

const { host } = vi.hoisted(() => ({
  host: {} as Record<string, unknown>
}))

vi.mock('@hermes/plugin-sdk', async () => pluginSdkMock(host))

const MEMBERS: GroupMember[] = [
  { connectionId: 'gateway-a', name: 'research', sourceScoped: true, targetProfile: 'research' },
  { connectionId: 'gateway-a', name: 'builder', sourceScoped: true, targetProfile: 'builder' }
]

function room(overrides: Partial<GroupChat> = {}): GroupChat {
  return {
    continuityMode: 'gateway',
    hosted: 'install:home',
    hostedConnectionId: 'gateway-a',
    hostedEpoch: 1,
    hostedSeq: 0,
    log: [],
    members: MEMBERS,
    roomId: 'room-1',
    watermarks: {},
    ...overrides
  }
}

async function loadRuntime(
  handler: (method: string, params: Record<string, unknown>, route: Record<string, unknown>) => unknown,
  routes: Record<string, unknown>[] = [
    { connectionId: 'gateway-a', mode: 'remote', profile: 'default', targetProfile: 'default' }
  ]
) {
  vi.resetModules()
  const calls: Array<{ connectionId: string; method: string; params: Record<string, unknown> }> = []
  const values = new Map<string, unknown>()

  for (const key of Object.keys(host)) {
    delete host[key]
  }

  Object.assign(host, {
    activeConnectionId: () => String(routes[0]?.connectionId || ''),
    notify: vi.fn(),
    profileRoutes: async () => routes,
    requestProfile: async (route: Record<string, unknown>, method: string, params: Record<string, unknown>) => {
      const call = { connectionId: String(route.connectionId || ''), method, params }

      calls.push(call)

      return handler(method, params, route)
    },
    state: {
      connectionId: { get: () => String(routes[0]?.connectionId || ''), listen: () => () => undefined },
      gateway: { get: () => 'open', listen: () => () => undefined },
      profile: { get: () => 'default', listen: () => () => undefined }
    }
  })

  const [chat, rounds, runtime, turns, shared] = await Promise.all([
    import('./group-chat'),
    import('./group-rounds'),
    import('./hosted-room-runtime'),
    import('./group-turns'),
    import('./shared')
  ])

  shared.setPluginCtx(scriptedStorage(values))

  return {
    calls,
    chat: chat as typeof groupChat,
    rounds: rounds as typeof groupRounds,
    runtime: runtime as typeof hostedRuntime,
    storage: scriptedStorage(values).storage,
    turns: turns as typeof groupTurns,
    values
  }
}

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
})

describe('hosted Group Chat client safety', () => {
  it('requires file parity before automatic hosting', async () => {
    const loaded = await loadRuntime(method => {
      if (method === 'groups.capabilities') {
        return { attachments: false, authority_gateway_id: 'install:home', driver: true, persistent_process: true }
      }

      throw new Error(`unexpected method: ${method}`)
    })

    await expect(loaded.runtime.probeHostedRoomMembers(MEMBERS)).resolves.toMatchObject({
      attachmentParity: false,
      eligible: true
    })
  })

  it('keeps FIFO inside one room while another room can continue', async () => {
    vi.useFakeTimers()

    const loaded = await loadRuntime((method, params) => {
      if (method === 'groups.capabilities') {
        return { attachments: true, authority_gateway_id: 'install:home', driver: true, persistent_process: true }
      }

      if (method === 'groups.list') {
        return { rooms: [] }
      }

      if (method === 'groups.send' && params.event_id === 'send-a') {
        throw new Error('temporary outage')
      }

      if (method === 'groups.send') {
        return { accepted: true }
      }

      if (method === 'groups.stop') {
        throw new Error('Stop overtook the earlier send')
      }

      throw new Error(`unexpected method: ${method}`)
    })

    loaded.values.set('hosted-room-outbox-v1', {
      commands: [
        {
          authorityId: 'install:home',
          commandId: 'send-a',
          connectionId: 'gateway-a',
          kind: 'send',
          payload: {},
          roomId: 'room-1',
          status: 'pending'
        },
        {
          authorityId: 'install:home',
          commandId: 'stop-b',
          connectionId: 'gateway-a',
          kind: 'stop',
          payload: {},
          roomId: 'room-1',
          status: 'pending'
        },
        {
          authorityId: 'install:home',
          commandId: 'send-c',
          connectionId: 'gateway-a',
          kind: 'send',
          payload: {},
          roomId: 'room-2',
          status: 'pending'
        }
      ],
      version: 1
    })

    await loaded.runtime.startHostedRoomRuntime(loaded.storage)

    expect(loaded.calls.filter(call => call.method === 'groups.stop')).toHaveLength(0)
    expect(loaded.calls.filter(call => call.method === 'groups.send').map(call => call.params.event_id)).toEqual([
      'send-a',
      'send-c'
    ])
    expect(loaded.values.get('hosted-room-outbox-v1')).toMatchObject({
      commands: [{ commandId: 'send-a' }, { commandId: 'stop-b' }]
    })
    loaded.runtime.stopHostedRoomRuntime()
  })

  it('recovers a pending command through the authority current connection', async () => {
    vi.useFakeTimers()
    const routes = [{ connectionId: 'gateway-new', mode: 'remote', profile: 'default', targetProfile: 'default' }]

    const loaded = await loadRuntime((method, _params) => {
      if (method === 'groups.capabilities') {
        return { attachments: true, authority_gateway_id: 'install:home', driver: true, persistent_process: true }
      }

      if (method === 'groups.list') {
        return { rooms: [] }
      }

      if (method === 'groups.send') {
        return { accepted: true }
      }

      throw new Error(`unexpected method: ${method}`)
    }, routes)

    loaded.values.set('hosted-room-outbox-v1', {
      commands: [
        {
          authorityId: 'install:home',
          commandId: 'send-a',
          connectionId: 'gateway-old',
          kind: 'send',
          payload: {},
          roomId: 'room-1',
          status: 'pending'
        }
      ],
      version: 1
    })

    await loaded.runtime.startHostedRoomRuntime(loaded.storage)

    expect(loaded.calls.find(call => call.method === 'groups.send')?.connectionId).toBe('gateway-new')
    expect(loaded.values.get('hosted-room-outbox-v1')).toMatchObject({ commands: [] })
    loaded.runtime.stopHostedRoomRuntime()
  })

  it('keeps an unsupported hosted room read-only with its update guidance', async () => {
    const loaded = await loadRuntime(() => {
      throw new Error('unsupported room must not dispatch')
    })

    const unsupported = room({
      continuityIssue: null,
      hostedStatus: { label: 'Update Studio to keep this Group Chat running.', state: 'unsupported' }
    })

    loaded.chat.$groupChats.set({ Legacy: unsupported })

    expect(loaded.runtime.groupChatContinuityReady(unsupported)).toBe(false)
    expect(loaded.rounds.sendToGroupChat('Legacy', MEMBERS, 'Do not queue')).toBeNull()
    expect(loaded.chat.$groupChats.get().Legacy.continuityIssue).toBe('Update Studio to keep this Group Chat running.')
    expect(loaded.values.has('hosted-room-outbox-v1')).toBe(false)
  })

  it('mirrors and resolves an exact hosted approval from the room', async () => {
    vi.useFakeTimers()

    const loaded = await loadRuntime((method, params) => {
      if (method === 'groups.capabilities') {
        return {
          attachments: true,
          authority_gateway_id: 'install:home',
          driver: true,
          max_log_limit: 100,
          persistent_process: true
        }
      }

      if (method === 'groups.list') {
        return {
          rooms: [
            {
              authority_epoch: 1,
              authority_gateway_id: 'install:home',
              latest_seq: 0,
              members: [
                { handle: 'research', member_id: 'research', profile: 'research' },
                { handle: 'builder', member_id: 'builder', profile: 'builder' }
              ],
              name: 'Release',
              room_id: 'room-1'
            }
          ]
        }
      }

      if (method === 'groups.state') {
        return {
          driver_status: {
            pending_actions: [
              {
                approval: { choices: ['once', 'deny'], command: 'npm test', description: 'Run tests' },
                execution_generation: 2,
                kind: 'approval',
                member_id: 'builder',
                request_id: 'approval-1',
                task_id: 'task-1'
              }
            ],
            working: true
          },
          room: {
            authority_epoch: 1,
            authority_gateway_id: 'install:home',
            members: [
              { handle: 'research', member_id: 'research', profile: 'research' },
              { handle: 'builder', member_id: 'builder', profile: 'builder' }
            ],
            name: 'Release',
            room_id: 'room-1'
          }
        }
      }

      if (method === 'groups.log') {
        return { events: [], has_more: false, latest_seq: 0 }
      }

      if (method === 'groups.approve') {
        expect(params).toMatchObject({
          choice: 'once',
          execution_generation: 2,
          member_id: 'builder',
          request_id: 'approval-1',
          room_id: 'room-1',
          task_id: 'task-1'
        })

        return { approved: true }
      }

      throw new Error(`unexpected method: ${method}`)
    })

    await loaded.runtime.startHostedRoomRuntime(loaded.storage)
    const prompt = Object.values(loaded.chat.$groupClarify.get())[0]
    const member = loaded.chat.$groupChats.get().Release.members?.find(candidate => candidate.name === 'builder')

    expect(prompt).toMatchObject({
      command: 'npm test',
      hostedApproval: { executionGeneration: 2, memberId: 'builder', taskId: 'task-1' },
      kind: 'approval'
    })
    expect(member).toBeTruthy()
    await loaded.turns.answerGroupClarify(prompt, member!, 'once')
    expect(loaded.calls.filter(call => call.method === 'groups.approve')).toHaveLength(1)
    expect(Object.values(loaded.chat.$groupClarify.get())).toEqual([])
    loaded.runtime.stopHostedRoomRuntime()
  })
})
