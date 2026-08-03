const assert = require('node:assert/strict')
const net = require('node:net')
const { Duplex } = require('node:stream')
const test = require('node:test')

const { TICKET_TTL_MS, createEvaWsRelay } = require('./eva-ws-relay.cjs')

const BASE_URL = 'https://hermes-jackie-david.ecs.electricsheephq.com'

function fakeUpstream(statusCode = 101) {
  let observed = ''
  const tunneled = []
  const server = net.createServer(socket => {
    socket.once('data', chunk => {
      observed = chunk.toString('latin1')
      if (statusCode === 101) {
        socket.write(
          'HTTP/1.1 101 Switching Protocols\r\n' +
            'Upgrade: websocket\r\n' +
            'Connection: Upgrade\r\n' +
            'Sec-WebSocket-Accept: test\r\n\r\n'
        )
        socket.on('data', payload => tunneled.push(Buffer.from(payload)))
      } else {
        socket.end(`HTTP/1.1 ${statusCode} Unauthorized\r\nConnection: close\r\n\r\n`)
      }
    })
  })
  return {
    connect: () => {
      const address = server.address()
      return new Promise((resolve, reject) => {
        const socket = net.connect(address.port, '127.0.0.1')
        socket.once('connect', () => resolve(socket))
        socket.once('error', reject)
      })
    },
    observed: () => observed,
    tunneled: () => Buffer.concat(tunneled),
    start: () => new Promise(resolve => server.listen(0, '127.0.0.1', resolve)),
    stop: () => new Promise(resolve => server.close(resolve))
  }
}

function upgrade(localUrl, options = {}) {
  const url = new URL(localUrl)
  return new Promise((resolve, reject) => {
    const socket = net.connect(Number(url.port), url.hostname)
    let response = ''
    socket.once('connect', () => {
      socket.write(
        `GET ${url.pathname}${url.search} HTTP/1.1\r\n` +
          `Host: ${url.host}\r\n` +
          'Connection: Upgrade\r\n' +
          'Upgrade: websocket\r\n' +
          'Sec-WebSocket-Key: dGVzdC1rZXk=\r\n' +
          'Sec-WebSocket-Version: 13\r\n' +
          (options.extensions ? `Sec-WebSocket-Extensions: ${options.extensions}\r\n` : '') +
          'Origin: file://renderer\r\n\r\n'
      )
    })
    socket.on('data', chunk => {
      response += chunk.toString('latin1')
      if (response.includes('\r\n\r\n')) {
        resolve({ response, socket })
      }
    })
    socket.once('error', reject)
  })
}

function clientFrame(payload, { fin = true, mask = Buffer.from([0x12, 0x34, 0x56, 0x78]), opcode = 0x1 } = {}) {
  const body = Buffer.isBuffer(payload) ? payload : Buffer.from(payload)
  let length
  if (body.length < 126) {
    length = Buffer.from([0x80 | body.length])
  } else if (body.length <= 0xffff) {
    length = Buffer.alloc(3)
    length[0] = 0xfe
    length.writeUInt16BE(body.length, 1)
  } else {
    length = Buffer.alloc(9)
    length[0] = 0xff
    length.writeBigUInt64BE(BigInt(body.length), 1)
  }

  const masked = Buffer.from(body)
  for (let index = 0; index < masked.length; index += 1) {
    masked[index] ^= mask[index % 4]
  }
  return Buffer.concat([Buffer.from([(fin ? 0x80 : 0) | opcode]), length, mask, masked])
}

async function waitForTunnel(upstream, minimumLength) {
  for (let attempt = 0; attempt < 40 && upstream.tunneled().length < minimumLength; attempt += 1) {
    await new Promise(resolve => setTimeout(resolve, 5))
  }
}

async function waitForClose(socket) {
  if (socket.destroyed) return
  await new Promise(resolve => socket.once('close', resolve))
}

test('renderer gets a single-use loopback ticket and never the managed runtime secret', async t => {
  const upstream = fakeUpstream()
  await upstream.start()
  const relay = createEvaWsRelay({
    connectUpstream: () => upstream.connect(),
    getUpstream: async () => ({ baseUrl: BASE_URL, token: 'runtime-secret' }),
    randomBytes: () => Buffer.alloc(32, 7)
  })
  t.after(async () => {
    await relay.close()
    await upstream.stop()
  })

  const localUrl = await relay.mintTicket()
  assert.match(localUrl, /^ws:\/\/127\.0\.0\.1:\d+\/api\/ws\?ticket=/)
  assert.doesNotMatch(localUrl, /runtime-secret|ecs\.electricsheephq\.com|eva_session/)

  const first = await upgrade(localUrl, { extensions: 'permessage-deflate' })
  assert.match(first.response, /^HTTP\/1\.1 101/)
  first.socket.destroy()
  assert.match(upstream.observed(), /GET \/api\/ws\?eva_session=runtime-secret HTTP\/1\.1/)
  assert.doesNotMatch(upstream.observed(), /^Origin:/im)
  assert.doesNotMatch(upstream.observed(), /^Sec-WebSocket-Extensions:/im)

  const second = await upgrade(localUrl)
  assert.match(second.response, /^HTTP\/1\.1 401/)
  second.socket.destroy()
})

test('tickets are bound to the exact voice endpoint and assigned profile', async t => {
  const upstream = fakeUpstream()
  await upstream.start()
  const relay = createEvaWsRelay({
    connectUpstream: () => upstream.connect(),
    getUpstream: async () => ({ baseUrl: BASE_URL, generation: 4, token: 'runtime-secret' }),
    getGeneration: () => 4
  })
  t.after(async () => {
    await relay.close()
    await upstream.stop()
  })

  const localUrl = await relay.mintTicket({
    generation: 4,
    path: '/api/audio/speak-stream',
    profile: 'research'
  })
  assert.match(localUrl, /^ws:\/\/127\.0\.0\.1:\d+\/api\/audio\/speak-stream\?ticket=/)
  assert.doesNotMatch(localUrl, /research|runtime-secret|eva_session/)

  const result = await upgrade(localUrl)
  assert.match(result.response, /^HTTP\/1\.1 101/)
  result.socket.destroy()

  const requestLine = upstream.observed().split('\r\n', 1)[0]
  const upstreamUrl = new URL(`https://upstream.invalid${requestLine.split(' ')[1]}`)
  assert.equal(upstreamUrl.pathname, '/api/audio/speak-stream')
  assert.equal(upstreamUrl.searchParams.get('eva_session'), 'runtime-secret')
  assert.equal(upstreamUrl.searchParams.get('profile'), 'research')
})

test('a valid ticket cannot be moved to a different WebSocket endpoint', async t => {
  const relay = createEvaWsRelay({
    connectUpstream: async () => {
      throw new Error('cross-path requests must not dial upstream')
    },
    getUpstream: async () => ({ baseUrl: BASE_URL, token: 'runtime-secret' })
  })
  t.after(async () => relay.close())

  const localUrl = new URL(await relay.mintTicket({ path: '/api/ws' }))
  localUrl.pathname = '/api/audio/speak-stream'
  const crossed = await upgrade(localUrl.toString())
  assert.match(crossed.response, /^HTTP\/1\.1 401/)
  crossed.socket.destroy()

  localUrl.pathname = '/api/ws'
  const replay = await upgrade(localUrl.toString())
  assert.match(replay.response, /^HTTP\/1\.1 401/)
  replay.socket.destroy()
})

test('validated plugin tickets preserve the namespaced path and query upstream', async t => {
  const upstream = fakeUpstream()
  await upstream.start()
  const relay = createEvaWsRelay({
    connectUpstream: () => upstream.connect(),
    getUpstream: async () => ({ baseUrl: BASE_URL, token: 'runtime-secret' })
  })
  t.after(async () => {
    await relay.close()
    await upstream.stop()
  })

  const result = await upgrade(
    await relay.mintTicket({
      path: '/api/plugins/kanban/events?mode=live',
      profile: 'research'
    }),
    { extensions: 'permessage-deflate' }
  )
  assert.match(result.response, /^HTTP\/1\.1 101/)
  result.socket.destroy()

  const requestLine = upstream.observed().split('\r\n', 1)[0]
  const upstreamUrl = new URL(`https://upstream.invalid${requestLine.split(' ')[1]}`)
  assert.equal(upstreamUrl.pathname, '/api/plugins/kanban/events')
  assert.equal(upstreamUrl.searchParams.get('mode'), 'live')
  assert.equal(upstreamUrl.searchParams.get('profile'), 'research')
  assert.match(upstream.observed(), /^Sec-WebSocket-Extensions: permessage-deflate$/im)
})

test('relay only mints supported core and plugin WebSocket endpoints', async t => {
  const relay = createEvaWsRelay({
    getUpstream: async () => ({ baseUrl: BASE_URL, token: 'runtime-secret' })
  })
  t.after(async () => relay.close())

  await assert.doesNotReject(relay.mintTicket({ path: '/api/plugins/kanban/events?mode=live', profile: 'research' }))
  await assert.rejects(relay.mintTicket({ path: '/api/sessions' }), /unsupported WebSocket endpoint/)
  await assert.rejects(
    relay.mintTicket({ path: '/api/plugins/kanban/%252e%252e/other/events' }),
    /ambiguous WebSocket endpoint/
  )
  await assert.rejects(
    relay.mintTicket({ path: '/api/plugins/kanban/events?token=renderer-secret' }),
    /invalid WebSocket query/
  )
  await assert.rejects(relay.mintTicket({ path: '/api/plugins/bad%2Fid/events' }), /ambiguous WebSocket endpoint/)
  await assert.rejects(relay.mintTicket({ path: '/api/plugins/bad%252Fid/events' }), /ambiguous WebSocket endpoint/)
})

test('expired and stale-generation tickets fail before dialing upstream', async t => {
  let clock = 0
  let generation = 8
  let dials = 0
  const relay = createEvaWsRelay({
    connectUpstream: async () => {
      dials += 1
      throw new Error('expired or stale tickets must not dial upstream')
    },
    getGeneration: () => generation,
    getUpstream: async () => ({ baseUrl: BASE_URL, generation, token: 'runtime-secret' }),
    now: () => clock
  })
  t.after(async () => relay.close())

  const expiredUrl = await relay.mintTicket({ generation, path: '/api/ws' })
  clock = TICKET_TTL_MS
  const expired = await upgrade(expiredUrl)
  assert.match(expired.response, /^HTTP\/1\.1 401/)
  expired.socket.destroy()

  clock += 1
  const staleUrl = await relay.mintTicket({ generation, path: '/api/ws' })
  generation += 1
  const stale = await upgrade(staleUrl)
  assert.match(stale.response, /^HTTP\/1\.1 401/)
  stale.socket.destroy()
  assert.equal(dials, 0)
})

test('relay denies managed billing RPCs before they reach upstream', async t => {
  const upstream = fakeUpstream()
  await upstream.start()
  const events = []
  const relay = createEvaWsRelay({
    connectUpstream: () => upstream.connect(),
    getUpstream: async () => ({ baseUrl: BASE_URL, token: 'runtime-secret' }),
    onEvent: event => events.push(event)
  })
  t.after(async () => {
    await relay.close()
    await upstream.stop()
  })

  for (const method of [
    'billing.state',
    'billing.charge',
    'billing.auto_reload',
    'billing.step_up',
    'subscription.state',
    'subscription.change',
    'subscription.resume',
    'subscription.upgrade',
    'usage.bars',
    'billing.future_method',
    'subscription.future_method'
  ]) {
    const result = await upgrade(await relay.mintTicket())
    assert.match(result.response, /^HTTP\/1\.1 101/)
    result.socket.write(clientFrame(JSON.stringify({ id: 1, jsonrpc: '2.0', method, params: {} })))
    await waitForClose(result.socket)
  }

  assert.equal(upstream.tunneled().length, 0)
  assert.equal(events.filter(event => event === 'client_rpc_denied').length, 11)
})

test('relay passes an unknown future gateway RPC frame through unchanged', async t => {
  const upstream = fakeUpstream()
  await upstream.start()
  const relay = createEvaWsRelay({
    connectUpstream: () => upstream.connect(),
    getUpstream: async () => ({ baseUrl: BASE_URL, token: 'runtime-secret' })
  })
  t.after(async () => {
    await relay.close()
    await upstream.stop()
  })

  const result = await upgrade(await relay.mintTicket())
  assert.match(result.response, /^HTTP\/1\.1 101/)
  const futureFrame = clientFrame(
    JSON.stringify({
      id: 'future-1',
      jsonrpc: '2.0',
      method: 'future.gateway.rpc.v999',
      params: { opaque: 'payload' }
    })
  )
  result.socket.write(futureFrame)

  await waitForTunnel(upstream, futureFrame.length)
  assert.deepEqual(upstream.tunneled(), futureFrame)
  result.socket.destroy()
})

test('relay preserves fragmented binary gateway traffic and rejects fragmented text', async t => {
  const upstream = fakeUpstream()
  await upstream.start()
  const relay = createEvaWsRelay({
    connectUpstream: () => upstream.connect(),
    getUpstream: async () => ({ baseUrl: BASE_URL, token: 'runtime-secret' })
  })
  t.after(async () => {
    await relay.close()
    await upstream.stop()
  })

  const binary = await upgrade(await relay.mintTicket())
  const fragments = Buffer.concat([
    clientFrame(Buffer.from([1, 2, 3]), { fin: false, opcode: 0x2 }),
    clientFrame(Buffer.from([4, 5, 6]), { opcode: 0x0 })
  ])
  binary.socket.write(fragments)
  await waitForTunnel(upstream, fragments.length)
  assert.deepEqual(upstream.tunneled(), fragments)
  binary.socket.destroy()

  const text = await upgrade(await relay.mintTicket())
  text.socket.write(
    clientFrame('{"id":1,"jsonrpc":"2.0","method":"billing.state"', {
      fin: false,
      opcode: 0x1
    })
  )
  await waitForClose(text.socket)
  assert.deepEqual(upstream.tunneled(), fragments)
})

test('an upstream authentication rejection invalidates the managed enrollment', async t => {
  const upstream = fakeUpstream(401)
  await upstream.start()
  let rejected = 0
  const relay = createEvaWsRelay({
    connectUpstream: () => upstream.connect(),
    getUpstream: async () => ({ baseUrl: BASE_URL, token: 'stale-secret' }),
    onAuthRejected: () => {
      rejected += 1
    }
  })
  t.after(async () => {
    await relay.close()
    await upstream.stop()
  })

  const result = await upgrade(await relay.mintTicket())
  assert.match(result.response, /^HTTP\/1\.1 401/)
  result.socket.destroy()
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(rejected, 1)
})

test('an upstream connection that never completes fails within the setup deadline', async t => {
  const relay = createEvaWsRelay({
    connectUpstream: () => new Promise(() => undefined),
    getUpstream: async () => ({ baseUrl: BASE_URL, token: 'runtime-secret' }),
    upstreamSetupTimeoutMs: 10
  })
  t.after(async () => relay.close())

  const result = await upgrade(await relay.mintTicket())
  assert.match(result.response, /^HTTP\/1\.1 502/)
  result.socket.destroy()
})

test('an upstream reset after the WebSocket handshake closes the relay without crashing', async t => {
  class ResettingUpstream extends Duplex {
    _read() {}

    _write(_chunk, _encoding, callback) {
      this.push(
        'HTTP/1.1 101 Switching Protocols\r\n' +
          'Upgrade: websocket\r\n' +
          'Connection: Upgrade\r\n' +
          'Sec-WebSocket-Accept: test\r\n\r\n'
      )
      setImmediate(() => this.emit('error', new Error('post-handshake reset')))
      callback()
    }
  }

  const relay = createEvaWsRelay({
    connectUpstream: async () => new ResettingUpstream(),
    getUpstream: async () => ({ baseUrl: BASE_URL, token: 'runtime-secret' })
  })
  t.after(async () => relay.close())

  const result = await upgrade(await relay.mintTicket())
  assert.match(result.response, /^HTTP\/1\.1 101/)
  if (!result.socket.destroyed) {
    await new Promise(resolve => result.socket.once('close', resolve))
  }
  assert.equal(result.socket.destroyed, true)
})

test('relay emits only coarse handshake events', async t => {
  const events = []
  const upstream = fakeUpstream(403)
  await upstream.start()
  const relay = createEvaWsRelay({
    getUpstream: async () => ({ baseUrl: BASE_URL, token: 'secret-runtime-token' }),
    connectUpstream: () => upstream.connect(),
    onEvent: event => events.push(event)
  })
  t.after(async () => {
    await relay.close()
    await upstream.stop()
  })

  const result = await upgrade(await relay.mintTicket())
  assert.match(result.response, /^HTTP\/1\.1 401/)
  result.socket.destroy()
  await new Promise(resolve => setImmediate(resolve))

  assert.ok(events.some(event => /^upstream_handshake status=/.test(event)))
  assert.equal(events.join(' ').includes('secret-runtime-token'), false)
  assert.equal(events.join(' ').includes('ecs.electricsheephq.com'), false)
})
