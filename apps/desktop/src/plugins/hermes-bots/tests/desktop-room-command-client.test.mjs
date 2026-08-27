import assert from 'node:assert/strict'
import test from 'node:test'

import {
  desktopRoomDescriptors,
  desktopRoomIdentity,
  runDesktopRoomCommandCycle
} from '../desktop-room-command-client.js'


test('descriptors expose classic rooms only with stable identities', () => {
  const rooms = {
    Classic: { roomId: 'room-1', log: [] },
    Legacy: { log: [{ text: 'hi' }] },
    Hosted: { roomId: 'room-2', hosted: 'gateway-a', log: [] },
    Deleted: { roomId: 'room-3', tombstone: true, log: [] }
  }

  assert.equal(desktopRoomIdentity('Legacy', rooms.Legacy), 'name:Legacy')
  assert.deepEqual(desktopRoomDescriptors(rooms), [
    { name: 'Classic', roomId: 'room-1' },
    { name: 'Legacy', roomId: 'name:Legacy' }
  ])
})


test('one cycle claims, executes and completes commands per gateway', async () => {
  const calls = []
  const request = async (route, method, params) => {
    calls.push({ connectionId: route.connectionId, method, params })
    if (route.connectionId === 'old') throw new Error('method not found')
    if (method === 'groups.desktop.claim') {
      return {
        commands: [{
          command_id: 'messaging:1',
          room_id: 'room-1',
          action: 'send',
          payload: { message: 'hello' }
        }]
      }
    }
    return { command: { state: 'completed' } }
  }

  const outcomes = await runDesktopRoomCommandCycle({
    routes: [
      { connectionId: 'old' },
      { connectionId: 'current' },
      { connectionId: 'current' }
    ],
    consumerId: 'desktop:test',
    rooms: { Classic: { roomId: 'room-1', log: [] } },
    request,
    execute: async command => ({ thread_id: `thread:${command.command_id}` })
  })

  assert.deepEqual(outcomes, [
    { commandId: 'messaging:1', connectionId: 'current', success: true }
  ])
  const complete = calls.find(call => call.method === 'groups.desktop.complete')
  assert.equal(complete.params.success, true)
  assert.deepEqual(complete.params.result, { thread_id: 'thread:messaging:1' })
  assert.deepEqual(
    calls.filter(call => call.method === 'groups.desktop.claim').map(call => call.connectionId),
    ['old', 'current']
  )
})


test('execution failures are acknowledged without breaking later cycles', async () => {
  const completions = []
  const outcomes = await runDesktopRoomCommandCycle({
    routes: [{ connectionId: 'current' }],
    consumerId: 'desktop:test',
    rooms: { Classic: { roomId: 'room-1', log: [] } },
    request: async (_route, method, params) => {
      if (method === 'groups.desktop.claim') {
        return {
          commands: [{
            command_id: 'messaging:failed',
            room_id: 'room-1',
            action: 'send',
            payload: { message: 'hello' }
          }]
        }
      }
      completions.push(params)
      return {}
    },
    execute: async () => {
      throw new Error('room disappeared')
    }
  })

  assert.equal(outcomes[0].success, false)
  assert.equal(completions[0].success, false)
  assert.equal(completions[0].result.message, 'room disappeared')
})


test('an unscoped local gateway still receives compatibility commands', async () => {
  const calls = []
  await runDesktopRoomCommandCycle({
    routes: [{ connectionId: '' }],
    consumerId: 'desktop:test',
    rooms: { Local: { roomId: 'room-local', log: [] } },
    request: async (_route, method, params) => {
      calls.push({ method, params })
      return method === 'groups.desktop.claim' ? { commands: [] } : {}
    },
    execute: async () => ({})
  })

  assert.equal(calls[0].method, 'groups.desktop.claim')
  assert.deepEqual(calls[0].params.room_ids, ['room-local'])
})


test('no advertised rooms performs no gateway request', async () => {
  let calls = 0
  const outcomes = await runDesktopRoomCommandCycle({
    routes: [{ connectionId: 'current' }],
    consumerId: 'desktop:test',
    rooms: {},
    request: async () => {
      calls += 1
      return {}
    },
    execute: async () => ({})
  })

  assert.deepEqual(outcomes, [])
  assert.equal(calls, 0)
})


test('large room rosters are claimed in bounded pages', async () => {
  const claimSizes = []
  const rooms = Object.fromEntries(
    Array.from({ length: 260 }, (_, index) => [
      `Room ${index}`,
      { roomId: `room-${index}`, log: [] }
    ])
  )
  await runDesktopRoomCommandCycle({
    routes: [{ connectionId: 'current' }],
    consumerId: 'desktop:test',
    rooms,
    request: async (_route, method, params) => {
      if (method === 'groups.desktop.claim') claimSizes.push(params.room_ids.length)
      return { commands: [] }
    },
    execute: async () => ({})
  })

  assert.deepEqual(claimSizes, [128, 128, 4])
})


test('one push drains more than one eight-command claim page', async () => {
  const pending = Array.from({ length: 9 }, (_, index) => ({
    command_id: `messaging:${index}`,
    room_id: 'room-1',
    lease_token: `token:${index}`
  }))
  const completed = []
  const outcomes = await runDesktopRoomCommandCycle({
    routes: [{ connectionId: 'current' }],
    consumerId: 'desktop:test',
    rooms: { Classic: { roomId: 'room-1', log: [] } },
    request: async (_route, method, params) => {
      if (method === 'groups.desktop.claim') {
        return { commands: pending.splice(0, params.limit) }
      }
      if (method === 'groups.desktop.complete') completed.push(params.command_id)
      return {}
    },
    execute: async () => ({ settled: true })
  })

  assert.equal(outcomes.length, 9)
  assert.equal(completed.length, 9)
  assert.equal(pending.length, 0)
})


test('retryable execution leaves a claimed command unacknowledged', async () => {
  const methods = []
  const outcomes = await runDesktopRoomCommandCycle({
    routes: [{ connectionId: 'current' }],
    consumerId: 'desktop:test',
    rooms: { Classic: { roomId: 'room-1', log: [] } },
    request: async (_route, method) => {
      methods.push(method)
      return method === 'groups.desktop.claim'
        ? { commands: [{ command_id: 'messaging:later', room_id: 'room-1' }] }
        : {}
    },
    execute: async () => {
      const error = new Error('member offline')
      error.retryable = true
      throw error
    }
  })

  assert.deepEqual(methods, ['groups.desktop.claim'])
  assert.deepEqual(outcomes, [{
    commandId: 'messaging:later',
    connectionId: 'current',
    success: false,
    retryable: true
  }])
})


test('disposing after claim leaves the command leased for safe retry', async () => {
  const methods = []
  const outcomes = await runDesktopRoomCommandCycle({
    routes: [{ connectionId: 'current' }],
    consumerId: 'desktop:test',
    rooms: { Classic: { roomId: 'room-1', log: [] } },
    request: async (_route, method) => {
      methods.push(method)
      return method === 'groups.desktop.claim'
        ? { commands: [{ command_id: 'messaging:one', room_id: 'room-1' }] }
        : {}
    },
    execute: async () => {
      throw new Error('must not execute')
    },
    shouldContinue: () => false
  })

  assert.deepEqual(outcomes, [])
  assert.deepEqual(methods, ['groups.desktop.claim'])
})
