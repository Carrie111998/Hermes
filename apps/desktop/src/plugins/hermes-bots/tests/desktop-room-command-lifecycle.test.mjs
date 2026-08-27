import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const pluginSource = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function loadRetention() {
  const start = pluginSource.indexOf('function syncDesktopRoomCommandRetention(')
  const end = pluginSource.indexOf('function scheduleDesktopRoomCommandPump(', start)
  assert.ok(start > 0)
  assert.ok(end > start)

  const releases = []
  const context = {
    desktopRoomCommandDisposed: false,
    desktopRoomCommandRetentions: new Map(),
    retainCalls: [],
    releases,
    host: {
      retainProfileSocket: route => {
        context.retainCalls.push(route)
        return () => releases.push(route)
      }
    }
  }
  vm.createContext(context)
  vm.runInContext(
    `${pluginSource.slice(start, end)}
globalThis.syncRetention = syncDesktopRoomCommandRetention
globalThis.releaseRetention = releaseDesktopRoomCommandRetention`,
    context
  )
  return context
}

const connection = id => ({ id, route: { connectionId: id, targetProfile: 'default' } })

test('classic room command routes keep one reusable socket per gateway', () => {
  const context = loadRetention()
  const connections = [connection('a'), connection('b')]

  context.syncRetention(connections)
  context.syncRetention(connections)

  assert.equal(context.retainCalls.length, 2)
  assert.equal(context.desktopRoomCommandRetentions.size, 2)
})

test('classic room command retention releases removed routes and all routes on stop', () => {
  const context = loadRetention()
  context.syncRetention([connection('a'), connection('b')])
  context.syncRetention([connection('a')])

  assert.deepEqual(context.releases.map(route => route.connectionId), ['b'])
  context.releaseRetention()
  assert.deepEqual(context.releases.map(route => route.connectionId), ['b', 'a'])
})

test('push is feature-detected, disposed, and the minute poll is only a backstop', () => {
  const start = pluginSource.indexOf('function startDesktopRoomCommandPump(')
  const stop = pluginSource.indexOf('function stopDesktopRoomCommandPump(', start)
  const end = pluginSource.indexOf('async function flushGroupChatServerSync', stop)
  const lifecycle = pluginSource.slice(start, end)

  assert.match(lifecycle, /desktop_rooms\.commands\.pending/)
  assert.match(lifecycle, /typeof host\.onEvent === 'function'/)
  assert.match(lifecycle, /desktopRoomCommandPushUnsub\(\)/)
  assert.match(pluginSource, /DESKTOP_ROOM_COMMAND_INTERVAL_MS = 60_000/)
  assert.match(lifecycle, /setInterval\(/)
  assert.match(lifecycle, /releaseDesktopRoomCommandRetention\(\)/)
})

test('a signal racing an active command cycle schedules a follow-up pass', () => {
  const start = pluginSource.indexOf('async function runDesktopRoomCommandPump(')
  const end = pluginSource.indexOf('function startDesktopRoomCommandPump(', start)
  const pump = pluginSource.slice(start, end)

  assert.match(pump, /if \(desktopRoomCommandRunning\)/)
  assert.match(pump, /desktopRoomCommandRerun = true/)
  assert.match(pump, /if \(desktopRoomCommandRerun && !desktopRoomCommandDisposed\)/)
  assert.match(pump, /scheduleDesktopRoomCommandPump\(/)
})

test('long room turns renew their generation-fenced command lease', () => {
  const clientSource = readFileSync(
    new URL('../desktop-room-command-client.js', import.meta.url),
    'utf8'
  )

  assert.match(clientSource, /groups\.desktop\.renew/)
  assert.match(clientSource, /lease_token: leaseToken/)
  assert.match(clientSource, /LEASE_RENEW_INTERVAL_MS = 15_000/)
  assert.match(clientSource, /clearInterval\(renewTimer\)/)
})

test('Stop commands use an independent priority lane', () => {
  const start = pluginSource.indexOf('function scheduleDesktopRoomCommandPump(')
  const end = pluginSource.indexOf('function stopDesktopRoomCommandPump(', start)
  const lifecycle = pluginSource.slice(start, end)

  assert.match(lifecycle, /runDesktopRoomStopPump/)
  assert.match(lifecycle, /actions: \['send'\]/)
  assert.match(lifecycle, /actions: \['stop'\]/)
  assert.match(lifecycle, /desktopRoomStopRunning/)
})
