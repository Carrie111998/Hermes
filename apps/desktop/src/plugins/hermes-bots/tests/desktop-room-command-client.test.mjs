import assert from 'node:assert/strict'
import test from 'node:test'

import {
  desktopRoomDescriptors,
  desktopRoomIdentity,
  runDesktopRoomCommandCycle
} from '../desktop-room-command-client.js'


test('descriptors expose classic rooms only with stable identities', () => {
  const rooms = {
    Classic: { roomId: 'room-1', desktopAuthorityToken: 'authority:classic', log: [] },
    Legacy: { desktopAuthorityToken: 'authority:legacy', log: [{ text: 'hi' }] },
    Hosted: { roomId: 'room-2', hosted: 'gateway-a', log: [] },
    Deleted: { roomId: 'room-3', tombstone: true, log: [] }
  }

  assert.equal(desktopRoomIdentity('Legacy', rooms.Legacy), 'name:Legacy')
  assert.deepEqual(desktopRoomDescriptors(rooms), [
    { name: 'Classic', roomId: 'room-1', authorityToken: 'authority:classic' },
    { name: 'Legacy', roomId: 'name:Legacy', authorityToken: 'authority:legacy' }
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
    rooms: { Classic: { roomId: 'room-1', desktopAuthorityToken: 'authority:test', log: [] } },
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


test('execution context reads command data from the gateway that issued the claim', async () => {
  const calls = []
  let executionContext
  await runDesktopRoomCommandCycle({
    routes: [{ connectionId: 'gateway-b' }],
    consumerId: 'desktop:test',
    rooms: {
      Classic: {
        roomId: 'room-1',
        desktopAuthorityToken: 'authority:test',
        log: []
      }
    },
    request: async (route, method, params) => {
      calls.push({ connectionId: route.connectionId, method, params })
      if (method === 'groups.desktop.claim') {
        return {
          commands: [{
            command_id: 'messaging:attachment',
            room_id: 'room-1',
            action: 'send',
            lease_token: 'lease:one',
            payload: { message: 'inspect' }
          }]
        }
      }
      return {}
    },
    execute: async (_command, _rooms, context) => {
      executionContext = context
      await context.request('groups.attachment.read', { attachment_id: 'att_1' })
      return { settled: true }
    }
  })

  assert.equal(executionContext.consumerId, 'desktop:test')
  assert.equal(executionContext.route.connectionId, 'gateway-b')
  const read = calls.find(call => call.method === 'groups.attachment.read')
  assert.equal(read.connectionId, 'gateway-b')
})


test('execution failures are acknowledged without breaking later cycles', async () => {
  const completions = []
  const outcomes = await runDesktopRoomCommandCycle({
    routes: [{ connectionId: 'current' }],
    consumerId: 'desktop:test',
    rooms: { Classic: { roomId: 'room-1', desktopAuthorityToken: 'authority:test', log: [] } },
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
      if (method === 'groups.desktop.complete') completions.push(params)
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
    rooms: { Local: { roomId: 'room-local', desktopAuthorityToken: 'authority:local', log: [] } },
    request: async (_route, method, params) => {
      calls.push({ method, params })
      return method === 'groups.desktop.claim' ? { commands: [] } : {}
    },
    execute: async () => ({})
  })

  assert.equal(calls[0].method, 'groups.desktop.claim')
  assert.deepEqual(calls[0].params.room_authorities, [
    { room_id: 'room-local', authority_token: 'authority:local' }
  ])
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
      { roomId: `room-${index}`, desktopAuthorityToken: `authority:${index}`, log: [] }
    ])
  )
  await runDesktopRoomCommandCycle({
    routes: [{ connectionId: 'current' }],
    consumerId: 'desktop:test',
    rooms,
    request: async (_route, method, params) => {
      if (method === 'groups.desktop.claim') claimSizes.push(params.room_authorities.length)
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
    rooms: { Classic: { roomId: 'room-1', desktopAuthorityToken: 'authority:test', log: [] } },
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
    rooms: { Classic: { roomId: 'room-1', desktopAuthorityToken: 'authority:test', log: [] } },
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
    rooms: { Classic: { roomId: 'room-1', desktopAuthorityToken: 'authority:test', log: [] } },
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


test('a Stop-only lane claims only Stop commands', async () => {
  const claims = []
  await runDesktopRoomCommandCycle({
    routes: [{ connectionId: 'current' }],
    consumerId: 'desktop:test',
    rooms: { Classic: { roomId: 'room-1', desktopAuthorityToken: 'authority:test', log: [] } },
    actions: ['stop'],
    request: async (_route, method, params) => {
      if (method === 'groups.desktop.claim') claims.push(params)
      return { commands: [] }
    },
    execute: async () => ({})
  })

  assert.deepEqual(claims[0].actions, ['stop'])
  assert.deepEqual(claims[0].room_authorities, [
    { room_id: 'room-1', authority_token: 'authority:test' }
  ])
})


test('lease renewal failure aborts execution and never acknowledges stale effects', async () => {
  const originalSetInterval = globalThis.setInterval
  const originalClearInterval = globalThis.clearInterval
  let renewTick = null
  let completed = 0
  globalThis.setInterval = callback => {
    renewTick = callback
    return 1
  }
  globalThis.clearInterval = () => undefined
  try {
    const cycle = runDesktopRoomCommandCycle({
      routes: [{ connectionId: 'current' }],
      consumerId: 'desktop:test',
      rooms: { Classic: { roomId: 'room-1', desktopAuthorityToken: 'authority:test', log: [] } },
      request: async (_route, method) => {
        if (method === 'groups.desktop.claim') {
          return {
            commands: [{
              command_id: 'messaging:lease-lost',
              room_id: 'room-1',
              action: 'send',
              lease_token: 'lease:one',
              payload: { message: 'hello' }
            }]
          }
        }
        if (method === 'groups.desktop.renew') throw new Error('lease moved')
        if (method === 'groups.desktop.complete') completed += 1
        return {}
      },
      execute: async (_command, _rooms, { signal }) =>
        new Promise((resolve, reject) => {
          signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true })
        })
    })
    while (renewTick === null) await Promise.resolve()
    renewTick()
    const outcomes = await cycle

    assert.equal(completed, 0)
    assert.equal(outcomes[0].leaseLost, true)
    assert.equal(outcomes[0].retryable, true)
  } finally {
    globalThis.setInterval = originalSetInterval
    globalThis.clearInterval = originalClearInterval
  }
})
