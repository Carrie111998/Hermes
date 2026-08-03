const crypto = require('node:crypto')
const http = require('node:http')
const tls = require('node:tls')

const { buildEvaManagedWsUrl } = require('./eva-managed.cjs')

const TICKET_TTL_MS = 30_000
const MAX_UPSTREAM_HEADER_BYTES = 64 * 1024
const UPSTREAM_SETUP_TIMEOUT_MS = 15_000

function safeDestroy(socket) {
  try {
    socket.destroy()
  } catch {
    // The peer already closed.
  }
}

function writeFailure(socket, statusCode, reason) {
  if (!socket || socket.destroyed) return
  const body = `${reason}\n`
  socket.end(
    `HTTP/1.1 ${statusCode} ${reason}\r\n` +
      'Connection: close\r\n' +
      'Content-Type: text/plain; charset=utf-8\r\n' +
      `Content-Length: ${Buffer.byteLength(body)}\r\n` +
      '\r\n' +
      body
  )
}

function buildUpgradeRequest(request, upstreamUrl) {
  const key = String(request.headers['sec-websocket-key'] || '')
  const version = String(request.headers['sec-websocket-version'] || '')
  if (!key || version !== '13') {
    throw new Error('Invalid WebSocket upgrade headers.')
  }

  const lines = [
    `GET ${upstreamUrl.pathname}${upstreamUrl.search} HTTP/1.1`,
    `Host: ${upstreamUrl.host}`,
    'Connection: Upgrade',
    'Upgrade: websocket',
    `Sec-WebSocket-Key: ${key}`,
    'Sec-WebSocket-Version: 13'
  ]
  const protocol = String(request.headers['sec-websocket-protocol'] || '').trim()
  const extensions = String(request.headers['sec-websocket-extensions'] || '').trim()
  if (protocol) lines.push(`Sec-WebSocket-Protocol: ${protocol}`)
  if (extensions) lines.push(`Sec-WebSocket-Extensions: ${extensions}`)
  return `${lines.join('\r\n')}\r\n\r\n`
}

function connectTls(upstreamUrl) {
  return new Promise((resolve, reject) => {
    const socket = tls.connect({
      host: upstreamUrl.hostname,
      port: Number(upstreamUrl.port || 443),
      rejectUnauthorized: true,
      servername: upstreamUrl.hostname
    })
    const fail = error => {
      socket.removeListener('secureConnect', ready)
      reject(error)
    }
    const ready = () => {
      socket.removeListener('error', fail)
      resolve(socket)
    }
    socket.once('error', fail)
    socket.once('secureConnect', ready)
  })
}

function createEvaWsRelay(options) {
  if (typeof options?.getUpstream !== 'function') {
    throw new TypeError('evaOS Agent WebSocket relay requires getUpstream().')
  }

  const now = options.now ?? (() => Date.now())
  const randomBytes = options.randomBytes ?? crypto.randomBytes
  const connectUpstream = options.connectUpstream ?? connectTls
  const upstreamSetupTimeoutMs = options.upstreamSetupTimeoutMs ?? UPSTREAM_SETUP_TIMEOUT_MS
  const onEvent = options.onEvent ?? (() => undefined)
  const tickets = new Map()
  const liveSockets = new Set()
  let server = null
  let startPromise = null

  function track(socket) {
    liveSockets.add(socket)
    socket.once('close', () => liveSockets.delete(socket))
    return socket
  }

  function pruneTickets() {
    const current = now()
    for (const [ticket, expiresAt] of tickets.entries()) {
      if (expiresAt <= current) tickets.delete(ticket)
    }
  }

  async function handleUpgrade(request, clientSocket, head) {
    track(clientSocket)
    let localUrl
    try {
      localUrl = new URL(request.url || '', 'http://127.0.0.1')
    } catch {
      writeFailure(clientSocket, 400, 'Bad Request')
      return
    }

    const keys = [...localUrl.searchParams.keys()]
    const ticket = localUrl.searchParams.get('ticket')
    pruneTickets()
    if (
      localUrl.pathname !== '/api/ws' ||
      keys.length !== 1 ||
      keys[0] !== 'ticket' ||
      !ticket ||
      !tickets.has(ticket)
    ) {
      writeFailure(clientSocket, 401, 'Unauthorized')
      return
    }

    const expiresAt = tickets.get(ticket)
    tickets.delete(ticket)
    if (!expiresAt || expiresAt <= now()) {
      writeFailure(clientSocket, 401, 'Unauthorized')
      return
    }

    let upstreamSocket
    let setupFinished = false
    const setupTimer = setTimeout(() => {
      if (setupFinished) return
      setupFinished = true
      onEvent('upstream_setup_timeout')
      writeFailure(clientSocket, 502, 'Bad Gateway')
      safeDestroy(upstreamSocket)
    }, upstreamSetupTimeoutMs)
    setupTimer.unref?.()
    try {
      const upstream = await options.getUpstream()
      const upstreamUrl = new URL(buildEvaManagedWsUrl(upstream.baseUrl, upstream.token))
      upstreamSocket = track(await connectUpstream(upstreamUrl))
      if (setupFinished) {
        safeDestroy(upstreamSocket)
        return
      }
      upstreamSocket.write(buildUpgradeRequest(request, upstreamUrl))
    } catch (error) {
      if (setupFinished) return
      setupFinished = true
      clearTimeout(setupTimer)
      onEvent(`upstream_connect_failed code=${String(error?.code || error?.name || 'unknown')}`)
      writeFailure(clientSocket, 502, 'Bad Gateway')
      safeDestroy(upstreamSocket)
      return
    }

    let header = Buffer.alloc(0)
    const failUpstream = () => {
      if (setupFinished) return
      setupFinished = true
      clearTimeout(setupTimer)
      if (!clientSocket.destroyed) writeFailure(clientSocket, 502, 'Bad Gateway')
      safeDestroy(upstreamSocket)
    }
    const onUpstreamData = chunk => {
      header = Buffer.concat([header, Buffer.from(chunk)])
      if (header.length > MAX_UPSTREAM_HEADER_BYTES) {
        failUpstream()
        return
      }
      const boundary = header.indexOf('\r\n\r\n')
      if (boundary < 0) return

      setupFinished = true
      clearTimeout(setupTimer)
      upstreamSocket.removeListener('data', onUpstreamData)
      upstreamSocket.removeListener('error', failUpstream)
      const responseHead = header.subarray(0, boundary + 4)
      const responseTail = header.subarray(boundary + 4)
      const statusMatch = /^HTTP\/1\.[01]\s+(\d{3})\b/.exec(responseHead.toString('latin1'))
      const statusCode = Number(statusMatch?.[1] || 0)
      onEvent(`upstream_handshake status=${statusCode || 'invalid'}`)

      if (statusCode === 401 || statusCode === 403) {
        Promise.resolve(options.onAuthRejected?.()).catch(() => undefined)
        writeFailure(clientSocket, 401, 'Unauthorized')
        safeDestroy(upstreamSocket)
        return
      }
      if (statusCode !== 101) {
        writeFailure(clientSocket, 502, 'Bad Gateway')
        safeDestroy(upstreamSocket)
        return
      }

      // The pre-handshake listener writes an HTTP error response, so replace it
      // once the socket has upgraded. Network resets after a successful 101 are
      // normal transport failures; leaving the upstream without an error
      // listener would turn one into an uncaught exception in Electron's main
      // process and terminate Eva.
      const closeUpgradedPair = () => {
        onEvent('upstream_disconnected')
        safeDestroy(clientSocket)
        safeDestroy(upstreamSocket)
      }
      upstreamSocket.on('error', closeUpgradedPair)
      upstreamSocket.once('close', () => safeDestroy(clientSocket))
      clientSocket.once('close', () => safeDestroy(upstreamSocket))

      clientSocket.write(responseHead)
      if (responseTail.length) clientSocket.write(responseTail)
      if (head?.length) upstreamSocket.write(head)
      clientSocket.pipe(upstreamSocket)
      upstreamSocket.pipe(clientSocket)
    }

    upstreamSocket.on('data', onUpstreamData)
    upstreamSocket.once('error', failUpstream)
    clientSocket.once('error', () => safeDestroy(upstreamSocket))
  }

  async function start() {
    if (server?.listening) return server.address()
    if (startPromise) return startPromise

    server = http.createServer((_request, response) => {
      response.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' })
      response.end('Not Found\n')
    })
    server.on('upgrade', (request, socket, head) => {
      void handleUpgrade(request, socket, head)
    })

    startPromise = new Promise((resolve, reject) => {
      const onError = error => {
        server.removeListener('listening', onListening)
        reject(error)
      }
      const onListening = () => {
        server.removeListener('error', onError)
        resolve(server.address())
      }
      server.once('error', onError)
      server.once('listening', onListening)
      server.listen(0, '127.0.0.1')
    }).finally(() => {
      startPromise = null
    })
    return startPromise
  }

  async function mintTicket() {
    const address = await start()
    pruneTickets()
    const ticket = randomBytes(32).toString('base64url')
    tickets.set(ticket, now() + TICKET_TTL_MS)
    return `ws://127.0.0.1:${address.port}/api/ws?ticket=${encodeURIComponent(ticket)}`
  }

  function disconnectAll() {
    tickets.clear()
    for (const socket of [...liveSockets]) safeDestroy(socket)
    liveSockets.clear()
  }

  async function close() {
    disconnectAll()
    if (!server) return
    const activeServer = server
    server = null
    await new Promise(resolve => activeServer.close(() => resolve()))
  }

  return { close, disconnectAll, mintTicket }
}

module.exports = {
  TICKET_TTL_MS,
  buildUpgradeRequest,
  createEvaWsRelay
}
