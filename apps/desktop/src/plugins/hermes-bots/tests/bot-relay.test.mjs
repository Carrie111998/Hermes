import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import test from 'node:test'

import {
  createBotRelaySupervisor,
  createBotRelayWorker,
  isMethodNotFound,
  RELAY_DELIVERY_REQUEST_TIMEOUT_MS,
  RELAY_GLOBAL_CONCURRENCY,
  relayTargetKey,
  retryDelaySeconds
} from '../bot-relay.mjs'

const NAMESPACE = 'namespace-test-1'
const COURIER = 'courier-test-1'

function route(connectionId, installId, profile = 'default') {
  return {
    connectionId,
    connectionLabel: connectionId.toUpperCase(),
    installId,
    mode: 'remote',
    profile,
    targetProfile: profile
  }
}

function relayError(code, message) {
  const error = new Error(message)
  error.code = code

  return error
}

function deferred() {
  let reject
  let resolve
  const promise = new Promise((onResolve, onReject) => {
    resolve = onResolve
    reject = onReject
  })

  return { promise, reject, resolve }
}

async function waitFor(predicate, message = 'condition was not reached') {
  const deadline = Date.now() + 1_000

  while (!predicate()) {
    if (Date.now() >= deadline) {
      throw new Error(message)
    }

    await new Promise(resolve => setTimeout(resolve, 1))
  }
}

function envelope(overrides = {}) {
  return {
    id: 'a'.repeat(32),
    body: 'ping',
    message: 'Message from 🤖 hermes (@hermes): ping',
    from_profile: 'default',
    from_handle: 'hermes',
    source_install_id: 'install-a',
    target_connection: 'b',
    target_profile: 'ops',
    target_install_id: 'install-b',
    courier_namespace_id: NAMESPACE,
    lease_owner: COURIER,
    lease_token: 'lease-a',
    lease_generation: 3,
    ...overrides
  }
}

function worker(host, options = {}) {
  return createBotRelayWorker({
    host,
    courierNamespaceId: NAMESPACE,
    courierId: COURIER,
    renewIntervalMs: 5,
    leaseSeconds: 1,
    ...options
  })
}

test('Desktop leadership permits one worker and transfers only after quiescence', async () => {
  const namespace = 'desktop-install-test'
  let leader = null
  let nextToken = 0
  let active = 0
  let maximumActive = 0
  const starts = []
  const namespaces = []
  const errors = []
  const firstStop = deferred()

  const leadershipFor = owner => ({
    acquire: async () => {
      if (leader) {
        return { acquired: false, retryAfterMs: 10 }
      }

      const leadershipToken = `token-${++nextToken}`
      leader = { owner, leadershipToken }
      return { acquired: true, courierNamespaceId: namespace, leadershipToken }
    },
    release: async leadershipToken => {
      const released = leader?.owner === owner && leader?.leadershipToken === leadershipToken

      if (released) leader = null
      return { released }
    }
  })

  const fakeWorker =
    (owner, stopGate = null) =>
    options => {
      let running = false

      return {
        start() {
          running = true
          active += 1
          maximumActive = Math.max(maximumActive, active)
          starts.push(owner)
          namespaces.push(options.courierNamespaceId)
        },
        async stop() {
          if (!running) return
          if (stopGate) await stopGate.promise
          running = false
          active -= 1
        }
      }
    }

  const first = createBotRelaySupervisor({
    host: {},
    leadership: leadershipFor('first'),
    createWorker: fakeWorker('first', firstStop),
    leadershipRetryMs: 10,
    onError: error => errors.push(error)
  })
  const second = createBotRelaySupervisor({
    host: {},
    leadership: leadershipFor('second'),
    createWorker: fakeWorker('second'),
    leadershipRetryMs: 10,
    onError: error => errors.push(error)
  })

  first.start()
  second.start()
  await waitFor(() => starts.length === 1)
  assert.deepEqual(starts, ['first'])

  const stopping = first.stop()
  await new Promise(resolve => setTimeout(resolve, 35))
  assert.equal(active, 1)
  assert.deepEqual(starts, ['first'])

  firstStop.resolve()
  await stopping
  await waitFor(() => starts.includes('second'))

  assert.equal(maximumActive, 1)
  assert.deepEqual(namespaces, [namespace, namespace])
  assert.deepEqual(errors, [])

  await second.stop()
  assert.equal(active, 0)
})

test('method-not-found detection is structured and narrow', () => {
  assert.equal(isMethodNotFound(relayError(-32601, 'Hermes RPC failed')), true)
  assert.equal(isMethodNotFound(new Error('method not found')), true)
  assert.equal(isMethodNotFound(relayError(5091, 'temporary claim failure')), false)
  assert.equal(isMethodNotFound(new Error('gateway not connected')), false)
})

test('target serialization key is qualified by namespace, install, connection, and profile', () => {
  const base = envelope()
  const key = relayTargetKey(base)

  assert.notEqual(key, relayTargetKey({ ...base, courier_namespace_id: 'other-namespace' }))
  assert.notEqual(key, relayTargetKey({ ...base, target_install_id: 'other-install' }))
  assert.notEqual(key, relayTargetKey({ ...base, target_connection: 'other-connection' }))
  assert.notEqual(key, relayTargetKey({ ...base, target_profile: 'other-profile' }))
})

test('retry backoff spans outages instead of burning the delivery budget', () => {
  assert.equal(retryDelaySeconds({ attempt: 1 }), 5)
  assert.equal(retryDelaySeconds({ attempt: 2 }), 10)
  assert.equal(retryDelaySeconds({ attempt: 6 }), 160)
  assert.equal(retryDelaySeconds({ attempt: 7 }), 300)
  assert.equal(retryDelaySeconds({ attempt: 100 }), 300)
})

test('roster sync binds remote rows and source gateway to the persisted namespace', async () => {
  const routes = [route('a', 'install-a'), route('b', 'install-b')]
  const syncs = []
  const host = {
    profileRoutes: async () => routes,
    requestProfile: async (target, method, params) => {
      if (method === 'profiles.list') {
        return {
          profiles: [
            {
              name: target.connectionId === 'a' ? 'default' : 'ops',
              description: `agent on ${target.connectionId}`
            }
          ]
        }
      }

      if (method === 'bot_relay.roster.sync') {
        syncs.push({ connection: target.connectionId, params })
        return { count: params.agents.length }
      }

      throw new Error(`unexpected method: ${method}`)
    }
  }

  await worker(host).syncRosters()

  assert.equal(syncs.length, 2)
  const a = syncs.find(call => call.connection === 'a').params
  assert.equal(a.courier_namespace_id, NAMESPACE)
  assert.equal(a.source_install_id, 'install-a')
  assert.deepEqual(a.agents, [
    {
      profile: 'ops',
      handle: 'ops',
      connection_id: 'b',
      connection_label: 'B',
      courier_namespace_id: NAMESPACE,
      target_install_id: 'install-b',
      title: '',
      description: 'agent on b'
    }
  ])
})

test('v2 claim renews its lease, forwards structured provenance, and ACKs one terminal outcome', async () => {
  const routes = [route('a', 'install-a'), route('b', 'install-b')]
  const delivery = deferred()
  const calls = []
  let claimed = false
  const host = {
    profileRoutes: async () => routes,
    requestProfile: async (target, method, params, timeoutMs) => {
      calls.push({ connection: target.connectionId, method, params, timeoutMs })

      if (method === 'bot_relay.outbox.claim') {
        if (target.connectionId !== 'a' || claimed) return { envelopes: [] }
        claimed = true
        // Source install identity is a Desktop route fact, not something the
        // sender-side enqueue path can reliably know.
        return { envelopes: [envelope({ source_install_id: '' })] }
      }

      if (method === 'bot_relay.outbox.renew') {
        return {
          generation: params.lease_generation,
          lease_expires_at: Date.now() / 1_000 + 1
        }
      }

      if (method === 'bot_relay.capabilities') {
        return { protocol_version: 2, durable_inbox: true }
      }
      if (method === 'bot_relay.deliver') return delivery.promise
      if (method === 'bot_relay.outbox.ack') return { ok: true }
      throw new Error(`unexpected method: ${method}`)
    }
  }
  const relay = worker(host)
  const run = relay.drainOnce()

  await waitFor(() => calls.some(call => call.method === 'bot_relay.deliver'), 'delivery never started')
  await waitFor(() => calls.some(call => call.method === 'bot_relay.outbox.renew'), 'lease heartbeat never renewed')
  delivery.resolve({
    protocol_version: 2,
    durable_receipt: true,
    status: 'completed',
    reply: 'pong'
  })
  await run

  const claim = calls.find(call => call.method === 'bot_relay.outbox.claim')
  assert.deepEqual(claim.params, {
    courier_id: COURIER,
    courier_namespace_id: NAMESPACE,
    lease_seconds: 1,
    limit: 1
  })

  const deliver = calls.find(call => call.method === 'bot_relay.deliver')
  assert.equal(deliver.connection, 'b')
  assert.equal(deliver.timeoutMs, RELAY_DELIVERY_REQUEST_TIMEOUT_MS)
  assert.deepEqual(deliver.params, {
    id: 'a'.repeat(32),
    body: 'ping',
    message: 'Message from 🤖 hermes (@hermes): ping',
    from_profile: 'default',
    from_handle: 'hermes',
    source_install_id: 'install-a',
    target_install_id: 'install-b',
    courier_namespace_id: NAMESPACE,
    profile: 'ops'
  })

  const ack = calls.find(call => call.method === 'bot_relay.outbox.ack')
  const expectedDigest = createHash('sha256')
    .update(`${'a'.repeat(32)}\u0000pong\u0000`)
    .digest('hex')
  assert.deepEqual(ack.params, {
    id: 'a'.repeat(32),
    courier_id: COURIER,
    courier_namespace_id: NAMESPACE,
    lease_token: 'lease-a',
    lease_generation: 3,
    reply: 'pong',
    error: '',
    outcome_digest: expectedDigest
  })
  assert.equal(
    calls.some(call => call.method === 'bot_relay.reply'),
    false
  )
})

test('v1 drain is used only for -32601 and preserves deliver → reply behavior', async () => {
  const routes = [route('a', 'install-a'), route('b', 'install-b')]
  const calls = []
  let drained = false
  const host = {
    profileRoutes: async () => routes,
    requestProfile: async (target, method, params) => {
      calls.push({ connection: target.connectionId, method, params })

      if (method === 'bot_relay.outbox.claim') {
        throw relayError(-32601, 'Method not found')
      }

      if (method === 'bot_relay.outbox.drain') {
        if (target.connectionId !== 'a' || drained) return { envelopes: [] }
        drained = true
        return { envelopes: [envelope({ courier_namespace_id: undefined })] }
      }

      if (method === 'bot_relay.deliver') return { reply: 'legacy pong' }
      if (method === 'bot_relay.reply') return { ok: true }
      throw new Error(`unexpected method: ${method}`)
    }
  }

  await worker(host).drainOnce()

  const deliver = calls.find(call => call.method === 'bot_relay.deliver')
  assert.deepEqual(deliver.params, {
    profile: 'ops',
    message: 'Message from 🤖 hermes (@hermes): ping'
  })
  const reply = calls.find(call => call.method === 'bot_relay.reply')
  assert.deepEqual(reply.params, { id: 'a'.repeat(32), reply: 'legacy pong' })
  assert.equal(
    calls.some(call => call.method === 'bot_relay.outbox.ack'),
    false
  )
  assert.equal(
    calls.some(call => call.method === 'bot_relay.outbox.nack'),
    false
  )
})

test('v2 source refuses to ACK a target that cannot prove a durable receipt', async () => {
  const routes = [route('a', 'install-a'), route('b', 'install-b')]
  const calls = []
  let claimed = false
  const host = {
    profileRoutes: async () => routes,
    requestProfile: async (target, method, params) => {
      calls.push({ connection: target.connectionId, method, params })

      if (method === 'bot_relay.outbox.claim') {
        if (target.connectionId !== 'a' || claimed) return { envelopes: [] }
        claimed = true
        return { envelopes: [envelope()] }
      }

      if (method === 'bot_relay.capabilities') {
        return { protocol_version: 2, durable_inbox: true }
      }
      // This is the response shape of a relay-v1 target. It may already have
      // run the turn, but it cannot make a replay idempotent.
      if (method === 'bot_relay.deliver') return { reply: 'legacy target reply' }
      if (method === 'bot_relay.outbox.nack') return { ok: true }
      throw new Error(`unexpected method: ${method}`)
    }
  }

  await worker(host).drainOnce()

  assert.equal(
    calls.some(call => call.method === 'bot_relay.outbox.ack'),
    false
  )
  const nack = calls.find(call => call.method === 'bot_relay.outbox.nack')
  assert.equal(nack.params.retryable, false)
  assert.match(nack.params.error, /outcome is indeterminate/)
})

test('a durable processing receipt is retried with the target-provided delay', async () => {
  const routes = [route('a', 'install-a'), route('b', 'install-b')]
  const calls = []
  let claimed = false
  const host = {
    profileRoutes: async () => routes,
    requestProfile: async (target, method, params) => {
      calls.push({ connection: target.connectionId, method, params })

      if (method === 'bot_relay.outbox.claim') {
        if (target.connectionId !== 'a' || claimed) return { envelopes: [] }
        claimed = true
        return { envelopes: [envelope()] }
      }

      if (method === 'bot_relay.capabilities') {
        return { protocol_version: 2, durable_inbox: true }
      }
      if (method === 'bot_relay.deliver') {
        return {
          protocol_version: 2,
          durable_receipt: true,
          status: 'processing',
          retry_after_seconds: 37
        }
      }

      if (method === 'bot_relay.outbox.nack') return { ok: true }
      throw new Error(`unexpected method: ${method}`)
    }
  }

  await worker(host).drainOnce()

  assert.equal(
    calls.some(call => call.method === 'bot_relay.outbox.ack'),
    false
  )
  const nack = calls.find(call => call.method === 'bot_relay.outbox.nack')
  assert.equal(nack.params.retryable, true)
  assert.equal(nack.params.retry_after_seconds, 37)
  assert.match(nack.params.error, /still processing/)
})

test('an indeterminate durable receipt retains its real terminal error', async () => {
  const routes = [route('a', 'install-a'), route('b', 'install-b')]
  const calls = []
  let claimed = false
  const host = {
    profileRoutes: async () => routes,
    requestProfile: async (target, method, params) => {
      calls.push({ connection: target.connectionId, method, params })

      if (method === 'bot_relay.outbox.claim') {
        if (target.connectionId !== 'a' || claimed) return { envelopes: [] }
        claimed = true
        return { envelopes: [envelope()] }
      }

      if (method === 'bot_relay.capabilities') {
        return { protocol_version: 2, durable_inbox: true }
      }
      if (method === 'bot_relay.deliver') {
        return {
          protocol_version: 2,
          durable_receipt: true,
          status: 'indeterminate',
          error: 'delivery turn timed out; target side effects are indeterminate'
        }
      }

      if (method === 'bot_relay.outbox.nack') return { ok: true }
      throw new Error(`unexpected method: ${method}`)
    }
  }

  await worker(host).drainOnce()

  assert.equal(
    calls.some(call => call.method === 'bot_relay.outbox.ack'),
    false
  )
  const nack = calls.find(call => call.method === 'bot_relay.outbox.nack')
  assert.equal(nack.params.retryable, false)
  assert.match(nack.params.error, /timed out.*side effects are indeterminate/)
})

test('a failed durable receipt is terminal even if the target suggests retry', async () => {
  const routes = [route('a', 'install-a'), route('b', 'install-b')]
  const calls = []
  let claimed = false
  const host = {
    profileRoutes: async () => routes,
    requestProfile: async (target, method, params) => {
      calls.push({ connection: target.connectionId, method, params })

      if (method === 'bot_relay.outbox.claim') {
        if (target.connectionId !== 'a' || claimed) return { envelopes: [] }
        claimed = true
        return { envelopes: [envelope()] }
      }

      if (method === 'bot_relay.capabilities') {
        return { protocol_version: 2, durable_inbox: true }
      }
      if (method === 'bot_relay.deliver') {
        return {
          protocol_version: 2,
          durable_receipt: true,
          status: 'failed',
          error: 'target turn failed',
          retryable: true
        }
      }

      if (method === 'bot_relay.outbox.nack') return { ok: true }
      throw new Error(`unexpected method: ${method}`)
    }
  }

  await worker(host).drainOnce()

  assert.equal(
    calls.some(call => call.method === 'bot_relay.outbox.ack'),
    false
  )
  const nack = calls.find(call => call.method === 'bot_relay.outbox.nack')
  assert.equal(nack.params.retryable, false)
  assert.equal(nack.params.error, 'target turn failed')
})

test('v2 source retains the same event until a legacy target upgrades', async () => {
  const routes = [route('a', 'install-a'), route('b', 'install-b')]
  const calls = []
  const eventId = 'c'.repeat(32)
  let available = true
  let claimCount = 0
  let pending = true
  let targetSupportsV2 = false
  const host = {
    profileRoutes: async () => routes,
    requestProfile: async (target, method, params) => {
      calls.push({ connection: target.connectionId, method, params })

      if (method === 'bot_relay.outbox.claim') {
        if (target.connectionId !== 'a' || !pending || !available) {
          return { envelopes: [] }
        }

        const claimed = envelope({
          attempt: claimCount,
          id: eventId,
          lease_generation: claimCount + 3,
          lease_token: `lease-${claimCount + 1}`
        })
        claimCount += 1
        available = false
        return { envelopes: [claimed] }
      }

      if (method === 'bot_relay.capabilities') {
        if (!targetSupportsV2) {
          throw relayError(-32601, 'Method not found')
        }

        return { protocol_version: 2, durable_inbox: true }
      }

      if (method === 'bot_relay.deliver') {
        return {
          protocol_version: 2,
          durable_receipt: true,
          status: 'completed',
          reply: `delivered:${params.id}`
        }
      }

      if (method === 'bot_relay.outbox.nack') {
        if (params.retryable) available = true
        return { ok: true }
      }

      if (method === 'bot_relay.outbox.renew') {
        return params
      }

      if (method === 'bot_relay.outbox.ack') {
        pending = false
        return { ok: true }
      }

      throw new Error(`unexpected method: ${method}`)
    }
  }
  const relay = worker(host)

  await relay.drainOnce()

  assert.equal(
    calls.some(call => call.method === 'bot_relay.deliver'),
    false
  )
  const firstNack = calls.find(call => call.method === 'bot_relay.outbox.nack')
  assert.equal(firstNack.params.id, eventId)
  assert.equal(firstNack.params.retryable, true)
  assert.match(firstNack.params.error, /durable inbox admission/)

  targetSupportsV2 = true
  await relay.drainOnce()
  await relay.drainOnce()

  const deliveries = calls.filter(call => call.method === 'bot_relay.deliver')
  assert.equal(deliveries.length, 1)
  assert.equal(deliveries[0].params.id, eventId)
  const acknowledgements = calls.filter(call => call.method === 'bot_relay.outbox.ack')
  assert.equal(acknowledgements.length, 1)
  assert.equal(acknowledgements[0].params.id, eventId)
  assert.equal(
    calls.filter(call => call.method === 'bot_relay.capabilities').length,
    2
  )
  assert.equal(
    calls.some(call => call.method === 'bot_relay.outbox.nack' && call.params.retryable === false),
    false
  )
  assert.equal(claimCount, 2)
  assert.equal(pending, false)
})

test('an explicit v1 capability response remains retryable without delivery', async () => {
  const routes = [route('a', 'install-a'), route('b', 'install-b')]
  const calls = []
  let claimed = false
  const host = {
    profileRoutes: async () => routes,
    requestProfile: async (target, method, params) => {
      calls.push({ connection: target.connectionId, method, params })

      if (method === 'bot_relay.outbox.claim') {
        if (target.connectionId !== 'a' || claimed) return { envelopes: [] }
        claimed = true
        return { envelopes: [envelope()] }
      }

      if (method === 'bot_relay.capabilities') {
        return { protocol_version: 1, durable_inbox: false }
      }

      if (method === 'bot_relay.outbox.nack') return { ok: true }
      throw new Error(`unexpected method: ${method}`)
    }
  }

  await worker(host).drainOnce()

  assert.equal(
    calls.some(call => call.method === 'bot_relay.deliver'),
    false
  )
  const nack = calls.find(call => call.method === 'bot_relay.outbox.nack')
  assert.equal(nack.params.retryable, true)
  assert.match(nack.params.error, /durable inbox admission/)
})

test('a transient capability preflight failure retries without invoking deliver', async () => {
  const routes = [route('a', 'install-a'), route('b', 'install-b')]
  const calls = []
  const errors = []
  let claimed = false
  const host = {
    profileRoutes: async () => routes,
    requestProfile: async (target, method, params) => {
      calls.push({ connection: target.connectionId, method, params })

      if (method === 'bot_relay.outbox.claim') {
        if (target.connectionId !== 'a' || claimed) return { envelopes: [] }
        claimed = true
        return { envelopes: [envelope()] }
      }

      if (method === 'bot_relay.capabilities') {
        throw relayError(5095, 'capability request temporarily unavailable')
      }

      if (method === 'bot_relay.outbox.nack') return { ok: true }
      throw new Error(`unexpected method: ${method}`)
    }
  }

  await worker(host, { onError: error => errors.push(error) }).drainOnce()

  assert.equal(
    calls.some(call => call.method === 'bot_relay.deliver'),
    false
  )
  const nack = calls.find(call => call.method === 'bot_relay.outbox.nack')
  assert.equal(nack.params.retryable, true)
  assert.equal(nack.params.retry_after_seconds, 5)
  assert.match(nack.params.error, /temporarily unavailable/)
  assert.equal(
    errors.some(error => error.code === 5095),
    true
  )
})

test('a lease lost during capability preflight never reaches target delivery', async () => {
  const routes = [route('a', 'install-a'), route('b', 'install-b')]
  const capability = deferred()
  const timers = []
  const cleared = []
  const calls = []
  const errors = []
  let claimed = false
  const host = {
    profileRoutes: async () => routes,
    requestProfile: async (target, method, params) => {
      calls.push({ connection: target.connectionId, method, params })

      if (method === 'bot_relay.outbox.claim') {
        if (target.connectionId !== 'a' || claimed) return { envelopes: [] }
        claimed = true
        return { envelopes: [envelope()] }
      }

      if (method === 'bot_relay.capabilities') return capability.promise
      if (method === 'bot_relay.outbox.renew') {
        throw relayError(4096, 'lease_mismatch')
      }

      throw new Error(`unexpected method: ${method}`)
    }
  }
  const relay = worker(host, {
    onError: error => errors.push(error),
    setIntervalFn: callback => {
      const timer = { callback }
      timers.push(timer)
      return timer
    },
    clearIntervalFn: timer => cleared.push(timer)
  })
  const run = relay.drainOnce()

  await waitFor(() => calls.some(call => call.method === 'bot_relay.capabilities'))
  assert.equal(timers.length, 1)
  timers[0].callback()
  await waitFor(() => errors.some(error => error.code === 4096))
  capability.resolve({ protocol_version: 2, durable_inbox: true })
  await run

  assert.equal(
    calls.some(call => call.method === 'bot_relay.deliver'),
    false
  )
  assert.equal(
    calls.some(call => call.method === 'bot_relay.outbox.ack'),
    false
  )
  assert.equal(
    calls.some(call => call.method === 'bot_relay.outbox.nack'),
    false
  )
  assert.deepEqual(cleared, timers)
})

test('a transient v2 claim failure never falls back to destructive v1 drain', async () => {
  const routes = [route('a', 'install-a'), route('b', 'install-b')]
  const calls = []
  const errors = []
  const host = {
    profileRoutes: async () => routes,
    requestProfile: async (target, method) => {
      calls.push({ connection: target.connectionId, method })

      if (method === 'bot_relay.outbox.claim' && target.connectionId === 'a') {
        throw relayError(5091, 'temporary claim failure')
      }

      if (method === 'bot_relay.outbox.claim') return { envelopes: [] }
      if (method === 'bot_relay.outbox.drain') throw new Error('v1 drain must not run')
      throw new Error(`unexpected method: ${method}`)
    }
  }

  await worker(host, { onError: error => errors.push(error) }).drainOnce()

  assert.equal(
    calls.some(call => call.method === 'bot_relay.outbox.drain'),
    false
  )
  assert.equal(
    calls.some(call => call.method === 'bot_relay.deliver'),
    false
  )
  assert.equal(
    errors.some(error => error.code === 5091),
    true
  )
})

test('dispatch is globally bounded at four and serial per target connection + profile', async () => {
  const routes = [route('a', 'install-a'), route('b', 'install-b'), route('c', 'install-c')]
  const keys = [
    ['b', 'ops'],
    ['b', 'research'],
    ['c', 'ops'],
    ['c', 'research'],
    ['b', 'ops'],
    ['b', 'research'],
    ['c', 'ops'],
    ['c', 'research']
  ]
  const rows = keys.map(([connection, profile], index) =>
    envelope({
      id: index.toString(16).padStart(32, '0'),
      target_connection: connection,
      target_profile: profile,
      target_install_id: connection === 'b' ? 'install-b' : 'install-c',
      lease_token: `lease-${index}`
    })
  )
  const pendingRows = [...rows]
  const claimLimits = []
  const capabilityCalls = new Map()
  let active = 0
  let maxActive = 0
  const activeByKey = new Map()
  let maxPerKey = 0
  const host = {
    profileRoutes: async () => routes,
    requestProfile: async (target, method, params) => {
      if (method === 'bot_relay.outbox.claim') {
        if (target.connectionId !== 'a') return { envelopes: [] }
        claimLimits.push(params.limit)
        return { envelopes: pendingRows.splice(0, params.limit) }
      }

      if (method === 'bot_relay.capabilities') {
        capabilityCalls.set(target.connectionId, (capabilityCalls.get(target.connectionId) || 0) + 1)
        return { protocol_version: 2, durable_inbox: true }
      }
      if (method === 'bot_relay.deliver') {
        const key = `${target.connectionId}:${params.profile}`
        active += 1
        maxActive = Math.max(maxActive, active)
        activeByKey.set(key, (activeByKey.get(key) || 0) + 1)
        maxPerKey = Math.max(maxPerKey, activeByKey.get(key))
        await new Promise(resolve => setTimeout(resolve, 10))
        active -= 1
        activeByKey.set(key, activeByKey.get(key) - 1)
        return {
          protocol_version: 2,
          durable_receipt: true,
          status: 'completed',
          reply: params.id
        }
      }

      if (method === 'bot_relay.outbox.ack') return { ok: true }
      if (method === 'bot_relay.outbox.renew') return params
      throw new Error(`unexpected method: ${method}`)
    }
  }

  const relay = worker(host, { renewIntervalMs: 10_000 })
  await relay.drainOnce()
  await relay.drainOnce()

  assert.equal(maxActive, RELAY_GLOBAL_CONCURRENCY)
  assert.equal(maxPerKey, 1)
  assert.deepEqual(new Set(claimLimits), new Set([1]))
  assert.equal(pendingRows.length, 0)
  // Cache lifetime is one drain pass: each target install is probed once per
  // batch even when several profiles execute concurrently on it.
  assert.deepEqual(
    capabilityCalls,
    new Map([
      ['b', 2],
      ['c', 2]
    ])
  )
})

test('claim rounds take one lease per sender before returning to the same source', async () => {
  const routes = [route('a', 'install-a'), route('b', 'install-b'), route('c', 'install-c')]
  const queues = new Map([
    [
      'a',
      [
        envelope({ id: '1'.repeat(32), target_connection: 'c', target_install_id: 'install-c' }),
        envelope({ id: '2'.repeat(32), target_connection: 'c', target_install_id: 'install-c' })
      ]
    ],
    [
      'b',
      [
        envelope({ id: '3'.repeat(32), target_connection: 'c', target_install_id: 'install-c' }),
        envelope({ id: '4'.repeat(32), target_connection: 'c', target_install_id: 'install-c' })
      ]
    ]
  ])
  const claimOrder = []
  const host = {
    profileRoutes: async () => routes,
    requestProfile: async (target, method, params) => {
      if (method === 'bot_relay.outbox.claim') {
        assert.equal(params.limit, 1)
        const item = queues.get(target.connectionId)?.shift()
        if (item) claimOrder.push(target.connectionId)
        return { envelopes: item ? [item] : [] }
      }

      if (method === 'bot_relay.capabilities') {
        return { protocol_version: 2, durable_inbox: true }
      }
      if (method === 'bot_relay.deliver') {
        return {
          protocol_version: 2,
          durable_receipt: true,
          status: 'completed',
          reply: params.id
        }
      }

      if (method === 'bot_relay.outbox.ack') return { ok: true }
      throw new Error(`unexpected method: ${method}`)
    }
  }

  await worker(host, { renewIntervalMs: 10_000 }).drainOnce()

  assert.deepEqual(claimOrder, ['a', 'b', 'a', 'b'])
})

test('target connection reuse is rejected before dispatch with a terminal NACK', async () => {
  const routes = [route('a', 'install-a'), route('b', 'replacement-install')]
  const calls = []
  let claimed = false
  const host = {
    profileRoutes: async () => routes,
    requestProfile: async (target, method, params) => {
      calls.push({ connection: target.connectionId, method, params })

      if (method === 'bot_relay.outbox.claim') {
        if (target.connectionId !== 'a' || claimed) return { envelopes: [] }
        claimed = true
        return { envelopes: [envelope()] }
      }

      if (method === 'bot_relay.outbox.nack') return { ok: true }
      throw new Error(`unexpected method: ${method}`)
    }
  }

  await worker(host).drainOnce()

  assert.equal(
    calls.some(call => call.method === 'bot_relay.deliver'),
    false
  )
  const nack = calls.find(call => call.method === 'bot_relay.outbox.nack')
  assert.equal(nack.connection, 'a')
  assert.equal(nack.params.courier_id, COURIER)
  assert.equal(nack.params.courier_namespace_id, NAMESPACE)
  assert.equal(nack.params.lease_token, 'lease-a')
  assert.equal(nack.params.lease_generation, 3)
  assert.equal(nack.params.retryable, false)
  assert.match(nack.params.error, /target install changed/)
})

test('an envelope leased to another courier fails closed without I/O or settlement', async () => {
  const routes = [route('a', 'install-a'), route('b', 'install-b')]
  const calls = []
  const errors = []
  let claimed = false
  const host = {
    profileRoutes: async () => routes,
    requestProfile: async (target, method, params) => {
      calls.push({ connection: target.connectionId, method, params })

      if (method === 'bot_relay.outbox.claim') {
        if (target.connectionId !== 'a' || claimed) return { envelopes: [] }
        claimed = true
        return { envelopes: [envelope({ lease_owner: 'other-courier' })] }
      }

      throw new Error(`unexpected method: ${method}`)
    }
  }

  await worker(host, { onError: error => errors.push(error) }).drainOnce()

  assert.equal(
    calls.some(call => call.method === 'bot_relay.deliver'),
    false
  )
  assert.equal(
    calls.some(call => call.method === 'bot_relay.outbox.ack'),
    false
  )
  assert.equal(
    calls.some(call => call.method === 'bot_relay.outbox.nack'),
    false
  )
  assert.match(errors[0].message, /not owned by this courier/)
})

test('stop lets an in-flight delivery renew and settle before clearing its heartbeat', async () => {
  const routes = [route('a', 'install-a'), route('b', 'install-b')]
  const delivery = deferred()
  const timers = []
  const cleared = []
  const calls = []
  let claimed = false
  const host = {
    profileRoutes: async () => routes,
    requestProfile: async (target, method, params) => {
      calls.push({ connection: target.connectionId, method, params })

      if (method === 'bot_relay.outbox.claim') {
        if (target.connectionId !== 'a' || claimed) return { envelopes: [] }
        claimed = true
        return { envelopes: [envelope()] }
      }

      if (method === 'bot_relay.capabilities') {
        return { protocol_version: 2, durable_inbox: true }
      }
      if (method === 'bot_relay.deliver') return delivery.promise
      throw new Error(`unexpected method: ${method}`)
    }
  }
  const relay = worker(host, {
    setIntervalFn: callback => {
      const timer = { callback }
      timers.push(timer)
      return timer
    },
    clearIntervalFn: timer => cleared.push(timer)
  })
  const run = relay.drainOnce()

  await waitFor(() => calls.some(call => call.method === 'bot_relay.deliver'))
  assert.equal(timers.length, 1)
  relay.stop()
  assert.deepEqual(cleared, [])
  delivery.resolve({
    protocol_version: 2,
    durable_receipt: true,
    status: 'completed',
    reply: 'late result'
  })
  await run

  assert.deepEqual(cleared, timers)
  assert.equal(
    calls.some(call => call.method === 'bot_relay.outbox.ack'),
    true
  )
  assert.equal(
    calls.some(call => call.method === 'bot_relay.outbox.nack'),
    false
  )
})

test('stop NACKs a leased-but-undispatched envelope instead of stranding it', async () => {
  const routes = [route('a', 'install-a'), route('b', 'install-b')]
  const first = envelope({ id: '1'.repeat(32), lease_token: 'lease-1' })
  const second = envelope({ id: '2'.repeat(32), lease_token: 'lease-2' })
  const queue = [first, second]
  const delivery = deferred()
  const calls = []
  const timers = []
  const cleared = []
  const host = {
    profileRoutes: async () => routes,
    requestProfile: async (target, method, params) => {
      calls.push({ connection: target.connectionId, method, params })

      if (method === 'bot_relay.outbox.claim') {
        if (target.connectionId !== 'a') return { envelopes: [] }
        const item = queue.shift()
        return { envelopes: item ? [item] : [] }
      }

      if (method === 'bot_relay.capabilities') {
        return { protocol_version: 2, durable_inbox: true }
      }
      if (method === 'bot_relay.deliver') return delivery.promise
      if (method === 'bot_relay.outbox.nack') return { ok: true }
      if (method === 'bot_relay.outbox.ack') return { ok: true }
      throw new Error(`unexpected method: ${method}`)
    }
  }
  const relay = worker(host, {
    setIntervalFn: callback => {
      const timer = { callback }
      timers.push(timer)
      return timer
    },
    clearIntervalFn: timer => cleared.push(timer)
  })
  const run = relay.drainOnce()

  await waitFor(() => timers.length === 2, 'second envelope was not leased')
  await waitFor(() => calls.some(call => call.method === 'bot_relay.deliver'))
  relay.stop()
  await waitFor(() => calls.some(call => call.method === 'bot_relay.outbox.nack'))
  await waitFor(() => cleared.includes(timers[1]))

  const nack = calls.find(call => call.method === 'bot_relay.outbox.nack')
  assert.equal(nack.params.id, second.id)
  assert.equal(nack.params.retryable, true)
  assert.match(nack.params.error, /stopped before dispatch/)
  assert.equal(cleared.includes(timers[1]), true)
  assert.equal(cleared.includes(timers[0]), false)

  delivery.resolve({
    protocol_version: 2,
    durable_receipt: true,
    status: 'completed',
    reply: 'done'
  })
  await run

  assert.equal(
    calls.some(call => call.method === 'bot_relay.outbox.ack'),
    true
  )
  assert.equal(cleared.includes(timers[0]), true)
})

test('worker stop is bounded and reports live authority when an RPC never settles', async () => {
  const pending = deferred()
  let requested = false
  const relay = worker(
    {
      profileRoutes: () => {
        requested = true
        return pending.promise
      },
      requestProfile: () => Promise.resolve(null)
    },
    { stopTimeoutMs: 10 }
  )
  const draining = relay.drainOnce()
  await waitFor(() => requested)

  const result = await Promise.race([
    relay.stop(),
    new Promise(resolve => setTimeout(() => resolve('stop timed out in test'), 100))
  ])
  assert.notEqual(result, 'stop timed out in test')
  assert.equal(result.status, 'timed_out_with_live_authority')

  pending.resolve([])
  await draining
})
