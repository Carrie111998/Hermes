const assert = require('node:assert/strict')
const net = require('node:net')
const { Duplex } = require('node:stream')
const test = require('node:test')

const { createEvaWsRelay } = require('./eva-ws-relay.cjs')

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

function upgrade(localUrl) {
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

  const first = await upgrade(localUrl)
  assert.match(first.response, /^HTTP\/1\.1 101/)
  first.socket.destroy()
  assert.match(upstream.observed(), /GET \/api\/ws\?eva_session=runtime-secret HTTP\/1\.1/)
  assert.doesNotMatch(upstream.observed(), /^Origin:/im)

  const second = await upgrade(localUrl)
  assert.match(second.response, /^HTTP\/1\.1 401/)
  second.socket.destroy()
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
  const futureFrame = Buffer.from('future.gateway.rpc.v999:opaque-payload')
  result.socket.write(futureFrame)

  for (let attempt = 0; attempt < 20 && upstream.tunneled().length < futureFrame.length; attempt += 1) {
    await new Promise(resolve => setTimeout(resolve, 5))
  }
  assert.deepEqual(upstream.tunneled(), futureFrame)
  result.socket.destroy()
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
