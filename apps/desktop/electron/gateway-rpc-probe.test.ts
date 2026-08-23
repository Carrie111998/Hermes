/**
 * Tests for electron/gateway-rpc-probe.ts.
 *
 * Run with: vitest run --project electron electron/gateway-rpc-probe.test.ts
 * (Wired into npm test:desktop:platforms in package.json.)
 *
 * The probe drives one JSON-RPC round-trip over a real WebSocket so boot can
 * tell "the gateway answers requests" from "the upgrade was accepted but the
 * dispatcher is dead" (#92927: a half-updated backend boots HTTP/WS-reachable
 * yet renders an empty shell). Here we inject a fake socket to replay each
 * outcome deterministically.
 */

import assert from 'node:assert/strict'

import { test } from 'vitest'

import { probeGatewayRpc } from './gateway-rpc-probe'

// Minimal WebSocket double: records listeners synchronously and exposes emit()
// plus a sent-frames log so tests can replay the server's side.
function makeFakeWs(): { FakeWs: new (url: string) => any; instances: any[] } {
  const instances: any[] = []

  class FakeWs {
    url: string
    closed = false
    sent: string[] = []
    listeners: Record<string, any[]> = {}
    constructor(url: string) {
      this.url = url
      this.listeners = {}
      this.closed = false
      instances.push(this)
    }
    addEventListener(type: string, fn: any) {
      ;(this.listeners[type] ||= []).push(fn)
    }
    send(data: string) {
      this.sent.push(data)
    }
    close() {
      this.closed = true
    }
    emit(type: string, event?: any) {
      for (const fn of this.listeners[type] || []) {
        fn(event)
      }
    }
  }

  return { FakeWs, instances }
}

const FAST = { connectTimeoutMs: 1_000, replyTimeoutMs: 1_000 }

function replyWithId(id: string, extra: Record<string, unknown> = {}) {
  return { data: JSON.stringify({ jsonrpc: '2.0', id, ...extra }) }
}

test('probe sends one JSON-RPC request on open and resolves on a result with the matching id', async () => {
  const { FakeWs, instances } = makeFakeWs()

  const promise = probeGatewayRpc('ws://host/api/ws?token=t', {
    WebSocketImpl: FakeWs,
    method: 'session.list',
    requestId: 'probe-1'
  })

  instances[0].emit('open')
  assert.equal(instances[0].sent.length, 1)

  const sent = JSON.parse(instances[0].sent[0])
  assert.equal(sent.jsonrpc, '2.0')
  assert.equal(sent.id, 'probe-1')
  assert.equal(sent.method, 'session.list')

  instances[0].emit('message', replyWithId('probe-1', { result: { sessions: [] } }))
  const result = await promise
  assert.deepEqual(result, { ok: true })
  assert.equal(instances[0].closed, true)
})

test('probe resolves ok on a JSON-RPC ERROR reply with the matching id (dispatcher round-trips)', async () => {
  const { FakeWs, instances } = makeFakeWs()
  const promise = probeGatewayRpc('ws://host/api/ws?token=t', { WebSocketImpl: FakeWs, requestId: 'probe-1' })

  instances[0].emit('open')
  // Unknown method on an older gateway: still a real dispatcher reply.
  instances[0].emit('message', replyWithId('probe-1', { error: { code: -32601, message: 'unknown method' } }))
  const result = await promise
  assert.deepEqual(result, { ok: true })
})

test('probe ignores gateway events and replies to other ids while waiting', async () => {
  const { FakeWs, instances } = makeFakeWs()

  const promise = probeGatewayRpc('ws://host/api/ws?token=t', {
    WebSocketImpl: FakeWs,
    requestId: 'probe-1',
    ...FAST
  })

  instances[0].emit('open')
  instances[0].emit('message', { data: JSON.stringify({ method: 'event', params: { type: 'gateway.ready' } }) })
  instances[0].emit('message', replyWithId('someone-else', { result: {} }))
  instances[0].emit('message', replyWithId('probe-1', { result: 'pong' }))
  const result = await promise
  assert.deepEqual(result, { ok: true })
})

test('probe does NOT resolve on events or replies whose id does not match — only our round-trip counts', async () => {
  const { FakeWs, instances } = makeFakeWs()

  const promise = probeGatewayRpc('ws://host/api/ws?token=t', {
    WebSocketImpl: FakeWs,
    requestId: 'probe-1',
    ...FAST
  })

  instances[0].emit('open')
  // A chatty gateway: ready event + a reply to someone else's request. If the
  // probe accepted ANY frame it would resolve here and never notice that ITS
  // request went unanswered — the exact half-broken-backend state #92927 is
  // about. It must keep waiting and fail when the socket closes unreplied.
  instances[0].emit('message', { data: JSON.stringify({ method: 'event', params: { type: 'gateway.ready' } }) })
  instances[0].emit('message', replyWithId('someone-else', { result: {} }))
  instances[0].emit('close', { code: 1000 })
  const result = await promise
  assert.equal(result.ok, false)
  assert.match(result.reason, /before replying/)
})

test('probe fails when the gateway closes after open without replying', async () => {
  const { FakeWs, instances } = makeFakeWs()
  const promise = probeGatewayRpc('ws://host/api/ws?token=t', { WebSocketImpl: FakeWs, method: 'session.list', ...FAST })

  instances[0].emit('open')
  instances[0].emit('close', { code: 1006 })
  const result = await promise
  assert.equal(result.ok, false)
  assert.match(result.reason, /before replying to "session.list"/)
  assert.match(result.reason, /1006/)
})

test('probe fails on the reply timeout when the upgrade is accepted but no reply ever comes', async () => {
  const { FakeWs, instances } = makeFakeWs()

  const promise = probeGatewayRpc('ws://host/api/ws?token=t', {
    WebSocketImpl: FakeWs,
    method: 'session.list',
    connectTimeoutMs: 1_000,
    replyTimeoutMs: 20
  })

  instances[0].emit('open')
  const result = await promise
  assert.equal(result.ok, false)
  assert.match(result.reason, /Timed out after 20ms waiting for a JSON-RPC reply/)
})

test('probe times out when the socket never opens', async () => {
  const { FakeWs } = makeFakeWs()

  const result = await probeGatewayRpc('ws://host/api/ws?token=t', {
    WebSocketImpl: FakeWs,
    connectTimeoutMs: 20,
    replyTimeoutMs: 1_000
  })

  assert.equal(result.ok, false)
  assert.match(result.reason, /Timed out after 20ms waiting for the WebSocket to open/)
})

test('probe fails gracefully when the constructor throws', async () => {
  class ThrowingWs {
    constructor() {
      throw new Error('boom')
    }
  }

  const result = await probeGatewayRpc('ws://host/api/ws?token=t', { WebSocketImpl: ThrowingWs })
  assert.equal(result.ok, false)
  assert.match(result.reason, /boom/)
})

test('probe fails when WebSocket is unavailable in the runtime', async () => {
  const result = await probeGatewayRpc('ws://host/api/ws?token=t', {})
  assert.equal(result.ok, false)
  assert.match(result.reason, /not available/)
})
