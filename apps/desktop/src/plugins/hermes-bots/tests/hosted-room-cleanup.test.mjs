import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const pluginSource = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

test('hosted projection removes rooms only from authenticated tombstones', () => {
  assert.match(pluginSource, /'groups\.list', \{ include_disbanded: true \}/)
  assert.match(pluginSource, /const disbandedRoomIds = new Set/)
  assert.match(pluginSource, /!disbandedRoomIds\.has\(String\(room\.roomId\)\)/)
  assert.match(pluginSource, /const collision = occupant && String\(occupant\?\.roomId \|\| ''\) !== roomId/)
  assert.match(pluginSource, /uniqueGroupChatName\(`\$\{roomName\} \(\$\{label\}\)`\.slice\(0, 64\), taken\)/)
})

test('hosted rooms never advertise a file drop target they will reject', () => {
  assert.match(pluginSource, /if \(!hostedAttachmentsUnavailable\) setDragOver\(true\)/)
  assert.doesNotMatch(pluginSource, /if \(hosted\) setDragOver\(true\)/)
})

test('same-gateway hosted rooms keep file staging while distributed rooms fail early', () => {
  assert.match(pluginSource, /async function stageSameGatewayHostedAttachments/)
  assert.match(pluginSource, /room\?\.continuityMode === 'distributed'/)
  assert.match(pluginSource, /await stageSameGatewayHostedAttachments\(group, members, sent\.id, attachments\)/)
  assert.match(pluginSource, /Files are not available across gateways yet\. The draft was kept\./)
})

test('hosted sends distinguish sending, queued offline, and working', () => {
  assert.match(pluginSource, /\{ state: 'sending', label: 'Sending…' \}/)
  assert.match(pluginSource, /\{ state: 'queued', label: 'Queued until the gateway reconnects' \}/)
  assert.match(pluginSource, /\{ state: 'working', label: 'Working' \}/)
  assert.match(pluginSource, /Your message is saved and will send when the gateway reconnects\./)
})

test('hosted approvals retain exact identity and use user-facing choices', () => {
  assert.match(pluginSource, /request_id: action\.request_id/)
  assert.match(pluginSource, /requestId: String\(action\.request_id \|\| ''\)/)
  assert.match(pluginSource, /once: 'Allow once', deny: 'Deny'/)
})

function load({ persisted = null, fail = false } = {}) {
  const values = new Map()
  const atom = initial => {
    const slot = {
      get: () => values.get(slot),
      set: value => values.set(slot, value),
      listen: () => () => undefined
    }
    values.set(slot, initial)
    return slot
  }
  const writes = new Map()
  const calls = []
  const routes = [
    { connectionId: 'home', targetProfile: 'default' },
    { connectionId: 'peer', targetProfile: 'builder' }
  ]
  const context = {
    atom,
    clearTimeout,
    setTimeout,
    host: {
      state: {
        profile: atom('default'),
        focusedSessionProfile: atom('default'),
        connectionId: atom('home')
      },
      profileRoutes: async () => routes,
      requestProfile: async (route, method, params) => {
        calls.push({ route, method, params })
        if (fail) throw new Error('gateway unavailable')
        return { ok: true }
      }
    },
    PALETTE_AREA: 'palette',
    COMPOSER_AREAS: { middleware: 'middleware' },
    document: { getElementById: () => null, createElement: () => ({}), head: { appendChild: () => undefined } }
  }
  const source = pluginSource
    .replace(/^import\s+\*\s+as\s+sdk\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^import\s+\{[\s\S]*?\}\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^const \{ McpTab, ToolsetConfigPanel \} = sdk\r?\n/m, '')
    .replace(/^import .* from 'react'\r?\n/m, '')
    .replace(/^import .* from 'react\/jsx-runtime'\r?\n/m, '')
    .replace('export default {', 'globalThis.plugin = {')
    .concat(`
      globalThis.__cleanup = {
        setContext(value) { pluginCtx = value },
        normalizeHostedRoomCleanup,
        addHostedRoomCleanup,
        armHostedRoomCleanup,
        releaseHostedRoomCleanup,
        dispatchHostedRoomCleanup,
        cleanup: $hostedRoomCleanup,
        dispose(value) { hostedRoomSyncDisposed = value }
      }
    `)
  vm.runInNewContext(source, context, { filename: 'plugin.js' })
  context.__cleanup.setContext({
    storage: {
      get: async key => (key === 'hosted-room-cleanup-v1' ? persisted : null),
      set: async (key, value) => writes.set(key, structuredClone(value))
    }
  })
  context.__cleanup.dispose(false)
  if (persisted) context.__cleanup.cleanup.set(context.__cleanup.normalizeHostedRoomCleanup(persisted))
  return { ...context.__cleanup, calls, writes }
}

test('failed setup cleanup is durable and revokes home plus peer scope', async () => {
  const harness = load()
  await harness.addHostedRoomCleanup({
    operationId: 'room-1:home',
    setupId: 'room-1',
    kind: 'home-disband',
    connectionId: 'home',
    roomId: 'room-1',
    cancelId: 'rollback-room-1'
  })
  await harness.addHostedRoomCleanup({
    operationId: 'room-1:peer',
    setupId: 'room-1',
    kind: 'peer-revoke',
    connectionId: 'peer',
    profile: 'builder',
    grant: 'signed-grant'
  })

  // Current-renderer operations remain fenced until setup either commits or
  // explicitly arms rollback.
  await harness.dispatchHostedRoomCleanup()
  assert.equal(harness.calls.length, 0)

  await harness.armHostedRoomCleanup('room-1')
  await harness.dispatchHostedRoomCleanup()
  assert.deepEqual(harness.calls.map(call => call.method), ['groups.disband', 'groups.peer.revoke'])
  assert.equal(harness.cleanup.get().operations.length, 0)
  assert.equal(harness.writes.get('hosted-room-cleanup-v1').operations.length, 0)
})

test('a restart recovers cleanup owned by the previous renderer', async () => {
  const persisted = {
    version: 1,
    operations: [{
      operationId: 'room-2:peer',
      setupId: 'room-2',
      kind: 'peer-revoke',
      connectionId: 'peer',
      ownerId: 'previous-renderer',
      profile: 'builder',
      grant: 'old-grant'
    }]
  }
  const harness = load({ persisted })
  await harness.dispatchHostedRoomCleanup()
  assert.equal(harness.calls.length, 1)
  assert.equal(harness.calls[0].method, 'groups.peer.revoke')
  assert.equal(harness.cleanup.get().operations.length, 0)
})

test('an unreachable gateway leaves exact cleanup work durable', async () => {
  const harness = load({ fail: true })
  await harness.addHostedRoomCleanup({
    operationId: 'room-3:home',
    setupId: 'room-3',
    kind: 'home-disband',
    connectionId: 'home',
    roomId: 'room-3',
    cancelId: 'rollback-room-3'
  })
  await harness.armHostedRoomCleanup('room-3')
  await harness.dispatchHostedRoomCleanup()
  assert.equal(harness.cleanup.get().operations.length, 1)
  assert.equal(harness.cleanup.get().operations[0].operationId, 'room-3:home')
})

test('successful setup release cannot leave a future cleanup behind', async () => {
  const harness = load()
  await harness.addHostedRoomCleanup({
    operationId: 'room-4:home',
    setupId: 'room-4',
    kind: 'home-disband',
    connectionId: 'home',
    roomId: 'room-4',
    cancelId: 'rollback-room-4'
  })
  await harness.releaseHostedRoomCleanup('room-4')
  await harness.dispatchHostedRoomCleanup()
  assert.equal(harness.calls.length, 0)
  assert.equal(harness.cleanup.get().operations.length, 0)
})
