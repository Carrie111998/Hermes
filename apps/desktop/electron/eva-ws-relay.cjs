const crypto = require('node:crypto')
const http = require('node:http')
const tls = require('node:tls')

const { buildEvaManagedWsUrl, isEvaManagedGatewayMethodBlocked } = require('./eva-managed.cjs')

const TICKET_TTL_MS = 30_000
const MAX_UPSTREAM_HEADER_BYTES = 64 * 1024
const UPSTREAM_SETUP_TIMEOUT_MS = 15_000
const MANAGED_PROFILE_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/
const PLUGIN_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/
const FORBIDDEN_ENDPOINT_QUERY_KEYS = new Set(['eva_session', 'profile', 'session_token', 'ticket', 'token'])

function hasAsciiControl(value) {
  return Array.from(value).some(character => {
    const codePoint = character.codePointAt(0)
    return codePoint === undefined || codePoint <= 0x1f || codePoint === 0x7f
  })
}

function normalizeEvaWsProfile(value) {
  if (value == null || String(value).trim() === '') return null
  const profile = String(value).trim()
  if (!MANAGED_PROFILE_RE.test(profile)) {
    throw new TypeError('evaOS Agent blocked an invalid Hermes profile.')
  }
  return profile
}

function normalizeEvaWsEndpoint(value = '/api/ws') {
  const raw = String(value || '')
  if (!raw.startsWith('/') || raw.startsWith('//') || raw.includes('\\') || raw.includes('#')) {
    throw new TypeError('evaOS Agent blocked an invalid WebSocket endpoint.')
  }

  const queryIndex = raw.indexOf('?')
  let pathname = queryIndex < 0 ? raw : raw.slice(0, queryIndex)
  const rawQuery = queryIndex < 0 ? '' : raw.slice(queryIndex + 1)
  const slashCount = Array.from(pathname).filter(character => character === '/').length
  if (/%(?:2f|5c)/i.test(pathname)) {
    throw new TypeError('evaOS Agent blocked an ambiguous WebSocket endpoint.')
  }
  for (let pass = 0; pass < 3; pass += 1) {
    let decoded
    try {
      decoded = decodeURIComponent(pathname)
    } catch {
      throw new TypeError('evaOS Agent blocked an invalid WebSocket endpoint.')
    }
    if (decoded.includes('\\') || Array.from(decoded).filter(character => character === '/').length !== slashCount) {
      throw new TypeError('evaOS Agent blocked an ambiguous WebSocket endpoint.')
    }
    if (decoded === pathname) break
    pathname = decoded
  }

  const segments = pathname.split('/')
  if (
    pathname.includes('%') ||
    pathname.includes('\\') ||
    pathname.includes('?') ||
    pathname.includes('#') ||
    hasAsciiControl(pathname) ||
    segments.slice(1).some(segment => segment === '.' || segment === '..' || segment === '')
  ) {
    throw new TypeError('evaOS Agent blocked an ambiguous WebSocket endpoint.')
  }

  pathname = `/${segments.slice(1).join('/')}`
  const pluginMatch = /^\/api\/plugins\/([^/]+)\/(.+)$/.exec(pathname)
  const allowed =
    pathname === '/api/ws' ||
    pathname === '/api/audio/speak-stream' ||
    Boolean(pluginMatch && PLUGIN_ID_RE.test(pluginMatch[1]))
  if (!allowed) {
    throw new TypeError('evaOS Agent blocked an unsupported WebSocket endpoint.')
  }

  const searchParams = new URLSearchParams(rawQuery)
  for (const [key, queryValue] of searchParams.entries()) {
    if (FORBIDDEN_ENDPOINT_QUERY_KEYS.has(key) || hasAsciiControl(key) || hasAsciiControl(queryValue)) {
      throw new TypeError('evaOS Agent blocked an invalid WebSocket query.')
    }
  }
  const search = searchParams.toString()
  return {
    pathname,
    search: search ? `?${search}` : '',
    path: `${pathname}${search ? `?${search}` : ''}`
  }
}

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

function buildUpgradeRequest(request, upstreamUrl, options = {}) {
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
  if (extensions && options.forwardExtensions !== false) lines.push(`Sec-WebSocket-Extensions: ${extensions}`)
  return `${lines.join('\r\n')}\r\n\r\n`
}

function containsBlockedGatewayMethod(payload) {
  let parsed
  try {
    parsed = JSON.parse(payload.toString('utf8'))
  } catch {
    return false
  }

  const messages = Array.isArray(parsed) ? parsed : [parsed]
  return messages.some(
    message =>
      message !== null &&
      typeof message === 'object' &&
      typeof message.method === 'string' &&
      isEvaManagedGatewayMethodBlocked(message.method)
  )
}

function createClientFrameGuard({ onFrame, onReject }) {
  let buffered = Buffer.alloc(0)
  let fragmentedOpcode = null

  return chunk => {
    if (chunk?.length) buffered = Buffer.concat([buffered, Buffer.from(chunk)])

    while (buffered.length >= 2) {
      const first = buffered[0]
      const second = buffered[1]
      const fin = (first & 0x80) !== 0
      const reserved = first & 0x70
      const opcode = first & 0x0f
      const masked = (second & 0x80) !== 0
      let payloadLength = second & 0x7f
      let headerLength = 2

      if (payloadLength === 126) {
        if (buffered.length < 4) return true
        payloadLength = buffered.readUInt16BE(2)
        headerLength = 4
      } else if (payloadLength === 127) {
        if (buffered.length < 10) return true
        const wideLength = buffered.readBigUInt64BE(2)
        if (wideLength > BigInt(Number.MAX_SAFE_INTEGER)) {
          onReject('client_frame_rejected')
          return false
        }
        payloadLength = Number(wideLength)
        headerLength = 10
      }

      const maskLength = masked ? 4 : 0
      if (payloadLength > Number.MAX_SAFE_INTEGER - headerLength - maskLength) {
        onReject('client_frame_rejected')
        return false
      }
      const frameLength = headerLength + maskLength + payloadLength
      if (buffered.length < frameLength) return true

      const frame = buffered.subarray(0, frameLength)
      buffered = buffered.subarray(frameLength)

      if (opcode === 0x1) {
        if (!fin || reserved !== 0 || fragmentedOpcode !== null) {
          onReject('client_text_frame_rejected')
          return false
        }

        const payloadOffset = headerLength + maskLength
        let payload = frame.subarray(payloadOffset)
        if (masked) {
          const mask = frame.subarray(headerLength, headerLength + 4)
          payload = Buffer.from(payload)
          for (let index = 0; index < payload.length; index += 1) {
            payload[index] ^= mask[index % 4]
          }
        }
        if (containsBlockedGatewayMethod(payload)) {
          onReject('client_rpc_denied')
          return false
        }
      } else if (opcode === 0x2) {
        if (fragmentedOpcode !== null) {
          onReject('client_frame_rejected')
          return false
        }
        if (!fin) fragmentedOpcode = opcode
      } else if (opcode === 0x0) {
        if (fragmentedOpcode === null) {
          onReject('client_frame_rejected')
          return false
        }
        if (fin) fragmentedOpcode = null
      }

      onFrame(frame)
    }

    return true
  }
}

function policyCloseFrame() {
  const payload = Buffer.alloc(2)
  payload.writeUInt16BE(1008)
  return Buffer.concat([Buffer.from([0x88, payload.length]), payload])
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
    for (const [ticket, grant] of tickets.entries()) {
      if (grant.expiresAt <= current) tickets.delete(ticket)
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

    const presentedTickets = localUrl.searchParams.getAll('ticket')
    const ticket = presentedTickets.length === 1 ? presentedTickets[0] : null
    pruneTickets()
    if (!ticket || !tickets.has(ticket)) {
      writeFailure(clientSocket, 401, 'Unauthorized')
      return
    }

    const grant = tickets.get(ticket)
    tickets.delete(ticket)
    if (!grant || grant.expiresAt <= now()) {
      writeFailure(clientSocket, 401, 'Unauthorized')
      return
    }

    localUrl.searchParams.delete('ticket')
    let requestedEndpoint
    try {
      const query = localUrl.searchParams.toString()
      requestedEndpoint = normalizeEvaWsEndpoint(`${localUrl.pathname}${query ? `?${query}` : ''}`)
    } catch {
      writeFailure(clientSocket, 401, 'Unauthorized')
      return
    }
    if (requestedEndpoint.path !== grant.endpoint.path) {
      writeFailure(clientSocket, 401, 'Unauthorized')
      return
    }
    if (
      grant.generation !== null &&
      typeof options.getGeneration === 'function' &&
      options.getGeneration() !== grant.generation
    ) {
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
      const upstream = await options.getUpstream({
        generation: grant.generation,
        path: grant.endpoint.path,
        profile: grant.profile
      })
      if (
        grant.expiresAt <= now() ||
        (grant.generation !== null && upstream?.generation != null && upstream.generation !== grant.generation)
      ) {
        setupFinished = true
        clearTimeout(setupTimer)
        writeFailure(clientSocket, 401, 'Unauthorized')
        return
      }
      const upstreamUrl = new URL(buildEvaManagedWsUrl(upstream.baseUrl, upstream.token))
      upstreamUrl.pathname = upstreamUrl.pathname.replace(/\/api\/ws$/, grant.endpoint.pathname)
      for (const [key, value] of new URLSearchParams(grant.endpoint.search).entries()) {
        upstreamUrl.searchParams.append(key, value)
      }
      if (grant.profile) upstreamUrl.searchParams.set('profile', grant.profile)
      upstreamSocket = track(await connectUpstream(upstreamUrl))
      if (setupFinished) {
        safeDestroy(upstreamSocket)
        return
      }
      const guardGatewayRpc = grant.endpoint.pathname === '/api/ws'
      upstreamSocket.write(buildUpgradeRequest(request, upstreamUrl, { forwardExtensions: !guardGatewayRpc }))
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
      const guardGatewayRpc = grant.endpoint.pathname === '/api/ws'
      if (guardGatewayRpc) {
        let rejected = false
        const rejectClientFrame = event => {
          if (rejected) return
          rejected = true
          onEvent(event)
          if (!clientSocket.destroyed) clientSocket.end(policyCloseFrame())
          safeDestroy(upstreamSocket)
        }
        const inspectClientFrames = createClientFrameGuard({
          onFrame: frame => {
            if (!upstreamSocket.write(frame)) {
              clientSocket.pause()
              upstreamSocket.once('drain', () => {
                if (!rejected && !clientSocket.destroyed) clientSocket.resume()
              })
            }
          },
          onReject: rejectClientFrame
        })
        if (head?.length) inspectClientFrames(head)
        if (!rejected) clientSocket.on('data', inspectClientFrames)
      } else {
        if (head?.length) upstreamSocket.write(head)
        clientSocket.pipe(upstreamSocket)
      }
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

  async function mintTicket(input = {}) {
    const address = await start()
    pruneTickets()
    const endpoint = normalizeEvaWsEndpoint(input.path)
    const profile = normalizeEvaWsProfile(input.profile)
    const generation =
      input.generation ?? (typeof options.getGeneration === 'function' ? options.getGeneration() : null)
    const ticket = randomBytes(32).toString('base64url')
    tickets.set(ticket, {
      endpoint,
      expiresAt: now() + TICKET_TTL_MS,
      generation,
      profile
    })
    const localUrl = new URL(`ws://127.0.0.1:${address.port}${endpoint.path}`)
    localUrl.searchParams.append('ticket', ticket)
    return localUrl.toString()
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
  createEvaWsRelay,
  normalizeEvaWsEndpoint,
  normalizeEvaWsProfile
}
