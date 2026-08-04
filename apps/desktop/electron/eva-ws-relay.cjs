const crypto = require('node:crypto')
const http = require('node:http')
const tls = require('node:tls')

const {
  buildEvaManagedWsUrl,
  isEvaManagedGatewayMethodBlocked,
  isEvaManagedGatewayRequestBlocked
} = require('./eva-managed.cjs')

const TICKET_TTL_MS = 30_000
const MAX_UPSTREAM_HEADER_BYTES = 64 * 1024
const UPSTREAM_SETUP_TIMEOUT_MS = 15_000
// Desktop backend contract v5 accepts one-shot file.attach requests up to the
// gateway's 384 MiB WebSocket ceiling. Keep the relay on the same bound while
// parsing incrementally so a renderer cannot make Electron buffer arbitrarily
// large frames.
const MAX_CLIENT_MESSAGE_BYTES = 384 * 1024 * 1024
const MANAGED_PROFILE_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/
const PLUGIN_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/
const FORBIDDEN_ENDPOINT_QUERY_KEYS = new Set(['eva_session', 'profile', 'session_token', 'ticket', 'token'])
const GENERIC_DISPATCH_GATEWAY_METHODS = new Set(['cli.exec', 'command.dispatch', 'slash.exec'])

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
  if (!socket) return
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

// Incrementally inspect top-level JSON-RPC objects (or objects in a root batch)
// without retaining bulk params. Strings are captured only when they can be an
// object key, an eligible object's method, or a supported generic-dispatch
// selector, so a 300+ MiB base64 file.attach payload adds only a small, fixed
// amount of policy memory.
function createGatewayRpcInspector({ onBlocked, onMethod }) {
  const decoder = new TextDecoder('utf-8')
  const stack = []
  let inString = false
  let escaped = false
  let captureMode = null
  let capturedString = ''
  let capturedValue = null
  let captureOverflow = false
  let inPrimitive = false

  const blockRpc = rpc => {
    if (rpc?.blocked) return
    if (rpc) {
      rpc.blocked = true
      if (rpc.batch) rpc.batch.blocked = true
    }
    onBlocked()
  }

  const parentStartsValue = token => {
    const parent = stack.at(-1)
    if (!parent) return
    if (parent.type === 'object' && parent.state === 'value') {
      if (token.type === 'string' && parent.eligible && parent.key === 'method' && typeof token.value === 'string') {
        parent.rpc.method = token.value
        if (isEvaManagedGatewayMethodBlocked(token.value)) blockRpc(parent.rpc)
        else if (!GENERIC_DISPATCH_GATEWAY_METHODS.has(token.value)) {
          parent.rpc.released = true
          if (parent.rpc.batch) parent.rpc.batch.releasePending = true
          else onMethod(token.value)
        }
      } else if (
        token.type === 'string' &&
        parent.role === 'params' &&
        (parent.key === 'command' || parent.key === 'name') &&
        typeof token.value === 'string'
      ) {
        parent.rpc.params[parent.key] = token.value
      }
      parent.key = null
      parent.state = 'commaOrEnd'
    } else if (parent.type === 'array' && parent.state === 'valueOrEnd') {
      if (parent.role === 'argv' && token.type === 'string' && typeof token.value === 'string') {
        parent.rpc.params.argv.push(token.value)
      }
      parent.state = 'commaOrEnd'
    }
  }

  const consumeToken = token => {
    const parent = stack.at(-1)

    if (token.type === '{') {
      const eligible =
        stack.length === 0 || (stack.length === 1 && parent?.type === 'array' && parent.rootBatch === true)
      const valueKey = parent?.type === 'object' && parent.state === 'value' ? parent.key : null
      const rpc = eligible
        ? {
            batch: parent?.rootBatch === true ? parent : null,
            blocked: false,
            method: null,
            params: {},
            released: false
          }
        : (parent?.rpc ?? null)
      const role = parent?.eligible && valueKey === 'params' ? 'params' : null
      parentStartsValue(token)
      stack.push({ eligible, key: null, role, rpc, state: 'keyOrEnd', type: 'object' })
      return
    }
    if (token.type === '[') {
      const rootBatch = stack.length === 0
      const valueKey = parent?.type === 'object' && parent.state === 'value' ? parent.key : null
      const rpc = parent?.rpc ?? null
      const role = parent?.role === 'params' && valueKey === 'argv' ? 'argv' : null
      if (role === 'argv') rpc.params.argv = []
      parentStartsValue(token)
      stack.push({ blocked: false, releasePending: false, role, rootBatch, rpc, state: 'valueOrEnd', type: 'array' })
      return
    }
    if (!parent) return

    if (parent.type === 'object') {
      if (token.type === '}' && (parent.state === 'keyOrEnd' || parent.state === 'commaOrEnd')) {
        const completed = stack.pop()
        if (completed.eligible && !completed.rpc.blocked) {
          if (isEvaManagedGatewayRequestBlocked(completed.rpc.method, completed.rpc.params)) {
            blockRpc(completed.rpc)
          } else if (!completed.rpc.released && typeof completed.rpc.method === 'string') {
            completed.rpc.released = true
            if (completed.rpc.batch) completed.rpc.batch.releasePending = true
            else onMethod(completed.rpc.method)
          }
        }
      } else if (parent.state === 'keyOrEnd' && token.type === 'string') {
        parent.key = token.value
        parent.state = 'colon'
      } else if (parent.state === 'colon' && token.type === ':') {
        parent.state = 'value'
      } else if (parent.state === 'value') {
        parentStartsValue(token)
      } else if (parent.state === 'commaOrEnd' && token.type === ',') {
        parent.state = 'keyOrEnd'
      }
      return
    }

    if (token.type === ']' && (parent.state === 'valueOrEnd' || parent.state === 'commaOrEnd')) {
      const completed = stack.pop()
      if (completed.rootBatch && completed.releasePending && !completed.blocked) onMethod('batch')
    } else if (parent.state === 'valueOrEnd') {
      parentStartsValue(token)
    } else if (parent.state === 'commaOrEnd' && token.type === ',') {
      parent.state = 'valueOrEnd'
    }
  }

  const stringCaptureMode = () => {
    const parent = stack.at(-1)
    if (parent?.type === 'object') {
      if (parent.state === 'keyOrEnd' || (parent.state === 'value' && parent.eligible && parent.key === 'method')) {
        return 'full'
      }
      if (parent.state === 'value' && parent.role === 'params' && (parent.key === 'command' || parent.key === 'name')) {
        return 'command'
      }
    }
    if (parent?.type === 'array' && parent.role === 'argv' && parent.state === 'valueOrEnd') return 'full'
    return null
  }

  const captureCommandPrefix = () => {
    let decoded
    try {
      decoded = JSON.parse(`"${capturedString}"`)
    } catch {
      return
    }

    const normalized = decoded.trimStart().replace(/^\/+/, '')
    if (!normalized) {
      capturedString = ''
      return
    }
    const separator = normalized.search(/\s/)
    if (separator >= 0) {
      capturedValue = normalized.slice(0, separator)
      capturedString = ''
    } else if (normalized.length > 128) {
      // Hidden managed command names are short. Once a first token exceeds
      // this bound it cannot later become one, so stop retaining it.
      capturedValue = normalized.slice(0, 129)
      capturedString = ''
    }
  }

  const finishString = () => {
    let value = capturedValue
    if (value === null && captureMode && !captureOverflow) {
      try {
        value = JSON.parse(`"${capturedString}"`)
      } catch {
        value = null
      }
    }
    consumeToken({ type: 'string', value })
    inString = false
    escaped = false
    captureMode = null
    capturedString = ''
    capturedValue = null
    captureOverflow = false
  }

  const consumeText = text => {
    for (let index = 0; index < text.length; index += 1) {
      const character = text[index]
      if (inString) {
        const terminatesString = !escaped && character === '"'
        if (captureMode && capturedValue === null && !captureOverflow && !terminatesString) {
          capturedString += character
          if (captureMode === 'command') captureCommandPrefix()
          if (capturedString.length > 512) {
            capturedString = ''
            captureOverflow = true
          }
        }
        if (escaped) {
          escaped = false
        } else if (character === '\\') {
          escaped = true
        } else if (character === '"') {
          finishString()
        }
        continue
      }

      if (inPrimitive) {
        if (!/[\s,}\]]/.test(character)) continue
        inPrimitive = false
        consumeToken({ type: 'primitive' })
        index -= 1
        continue
      }

      if (/\s/.test(character)) continue
      if (character === '"') {
        inString = true
        captureMode = stringCaptureMode()
        capturedString = ''
        capturedValue = null
        captureOverflow = false
      } else if ('{}[]:,'.includes(character)) {
        consumeToken({ type: character })
      } else {
        inPrimitive = true
      }
    }
  }

  return {
    finish() {
      consumeText(decoder.decode())
      if (inPrimitive) {
        inPrimitive = false
        consumeToken({ type: 'primitive' })
      }
    },
    push(bytes) {
      consumeText(decoder.decode(bytes, { stream: true }))
    }
  }
}

// Parse renderer frames incrementally and forward their raw bytes while
// inspecting gateway JSON-RPC text. The last payload byte of a completed text
// message is withheld until inspection finishes. A blocked method therefore
// never becomes a complete upstream WebSocket message, while allowed large
// frames never need to be copied into one giant Electron buffer.
function createClientFrameGuard({ onFrame, onReject }) {
  const PRELUDE_MAX_BYTES = 64 * 1024
  let header = Buffer.alloc(0)
  let frame = null
  let fragmentedOpcode = null
  let inspector = null
  let messageBytes = 0
  let methodSeen = false
  let prelude = []
  let preludeBytes = 0
  let rejected = false

  const reject = event => {
    if (rejected) return false
    rejected = true
    onReject(event)
    return false
  }

  const flushPrelude = () => {
    for (const chunk of prelude) onFrame(chunk)
    prelude = []
    preludeBytes = 0
  }

  const forwardInspected = chunk => {
    if (!chunk?.length) return
    if (methodSeen || preludeBytes + chunk.length > PRELUDE_MAX_BYTES) {
      flushPrelude()
      onFrame(chunk)
      return
    }
    prelude.push(chunk)
    preludeBytes += chunk.length
  }

  const finishTextMessage = tail => {
    inspector?.finish()
    inspector = null
    fragmentedOpcode = null
    messageBytes = 0
    if (rejected) return false
    flushPrelude()
    if (tail?.length) onFrame(tail)
    methodSeen = false
    return true
  }

  const beginFrame = rawHeader => {
    const first = rawHeader[0]
    const second = rawHeader[1]
    const fin = (first & 0x80) !== 0
    const reserved = first & 0x70
    const opcode = first & 0x0f
    const masked = (second & 0x80) !== 0
    const shortLength = second & 0x7f
    let payloadLength = shortLength
    let offset = 2
    if (shortLength === 126) {
      payloadLength = rawHeader.readUInt16BE(offset)
      offset += 2
    } else if (shortLength === 127) {
      const wideLength = rawHeader.readBigUInt64BE(offset)
      if (wideLength > BigInt(MAX_CLIENT_MESSAGE_BYTES)) return reject('client_frame_rejected')
      payloadLength = Number(wideLength)
      offset += 8
    }
    const mask = masked ? rawHeader.subarray(offset, offset + 4) : null

    if (!masked || reserved !== 0 || payloadLength > MAX_CLIENT_MESSAGE_BYTES) {
      return reject('client_frame_rejected')
    }
    if (opcode >= 0x8 && (!fin || payloadLength > 125)) {
      return reject('client_frame_rejected')
    }

    let inspectText = false
    if (opcode === 0x1) {
      if (fragmentedOpcode !== null) return reject('client_frame_rejected')
      methodSeen = false
      prelude = []
      preludeBytes = 0
      inspector = createGatewayRpcInspector({
        onBlocked: () => reject('client_rpc_denied'),
        onMethod: () => {
          methodSeen = true
          if (!rejected) flushPrelude()
        }
      })
      inspectText = true
      fragmentedOpcode = fin ? null : opcode
      messageBytes = payloadLength
    } else if (opcode === 0x2) {
      if (fragmentedOpcode !== null) return reject('client_frame_rejected')
      fragmentedOpcode = fin ? null : opcode
      messageBytes = payloadLength
    } else if (opcode === 0x0) {
      if (fragmentedOpcode === null) return reject('client_frame_rejected')
      inspectText = fragmentedOpcode === 0x1
      messageBytes += payloadLength
      if (messageBytes > MAX_CLIENT_MESSAGE_BYTES) return reject('client_frame_rejected')
    } else if (opcode < 0x8) {
      return reject('client_frame_rejected')
    }

    frame = {
      fin,
      inspectText,
      mask,
      maskOffset: 0,
      payloadRemaining: payloadLength
    }

    // Empty final text frames must be inspected before their header is sent;
    // non-empty frames are safe to stream because the payload remains
    // incomplete until finishTextMessage releases its held tail byte.
    if (!(inspectText && fin && payloadLength === 0)) {
      if (inspectText) forwardInspected(rawHeader)
      else onFrame(rawHeader)
    }
    if (payloadLength === 0) {
      frame = null
      if (inspectText && fin) {
        if (!finishTextMessage()) return false
        onFrame(rawHeader)
      } else if (fin && opcode === 0x0) {
        fragmentedOpcode = null
        messageBytes = 0
      }
    }
    return !rejected
  }

  return chunk => {
    if (rejected || !chunk?.length) return !rejected
    const input = Buffer.from(chunk)
    let offset = 0

    while (offset < input.length && !rejected) {
      if (!frame) {
        const neededPrefix = header.length < 2 ? 2 : 0
        if (neededPrefix) {
          const take = Math.min(neededPrefix - header.length, input.length - offset)
          header = Buffer.concat([header, input.subarray(offset, offset + take)])
          offset += take
          if (header.length < 2) break
        }
        const shortLength = header[1] & 0x7f
        const fullHeaderLength = 2 + (shortLength === 126 ? 2 : shortLength === 127 ? 8 : 0) + 4
        if (header.length < fullHeaderLength) {
          const take = Math.min(fullHeaderLength - header.length, input.length - offset)
          header = Buffer.concat([header, input.subarray(offset, offset + take)])
          offset += take
          if (header.length < fullHeaderLength) break
        }
        const rawHeader = header
        header = Buffer.alloc(0)
        if (!beginFrame(rawHeader)) return false
        if (!frame) continue
      }

      const take = Math.min(frame.payloadRemaining, input.length - offset)
      if (take === 0) break
      const raw = input.subarray(offset, offset + take)
      const completesFrame = take === frame.payloadRemaining
      const holdTail = frame.inspectText && frame.fin && completesFrame
      const forwarded = holdTail ? raw.subarray(0, -1) : raw
      const tail = holdTail ? raw.subarray(-1) : null

      if (frame.inspectText) {
        const decoded = Buffer.from(raw)
        for (let index = 0; index < decoded.length; index += 1) {
          decoded[index] ^= frame.mask[(frame.maskOffset + index) % 4]
        }
        inspector?.push(decoded)
      }
      if (rejected) return false
      if (forwarded.length) {
        if (frame.inspectText) forwardInspected(forwarded)
        else onFrame(forwarded)
      }

      frame.maskOffset = (frame.maskOffset + take) % 4
      frame.payloadRemaining -= take
      offset += take

      if (frame.payloadRemaining === 0) {
        const finalText = frame.inspectText && frame.fin
        const finalContinuation = frame.fin && fragmentedOpcode !== null
        frame = null
        if (finalText) {
          if (!finishTextMessage(tail)) return false
        } else if (finalContinuation) {
          fragmentedOpcode = null
          messageBytes = 0
        }
      }
    }

    return !rejected
  }
}

function policyCloseFrame() {
  const payload = Buffer.alloc(2)
  payload.writeUInt16BE(1008)
  return Buffer.concat([Buffer.from([0x88, payload.length]), payload])
}

function connectTls(upstreamUrl, timeoutMs = UPSTREAM_SETUP_TIMEOUT_MS, tlsConnect = tls.connect) {
  return new Promise((resolve, reject) => {
    const socket = tlsConnect({
      host: upstreamUrl.hostname,
      port: Number(upstreamUrl.port || 443),
      rejectUnauthorized: true,
      servername: upstreamUrl.hostname
    })
    let settled = false
    const timeout = setTimeout(() => {
      const error = Object.assign(new Error('Upstream TLS handshake timed out.'), { code: 'ETIMEDOUT' })
      fail(error)
    }, timeoutMs)
    timeout.unref?.()
    const cleanup = () => {
      clearTimeout(timeout)
      socket.removeListener('error', fail)
      socket.removeListener('secureConnect', ready)
    }
    const fail = error => {
      if (settled) return
      settled = true
      cleanup()
      safeDestroy(socket)
      reject(error)
    }
    const ready = () => {
      if (settled) return
      settled = true
      cleanup()
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
      if (!upstreamUrl.pathname.endsWith('/api/ws')) {
        throw new Error('Managed upstream base URL does not expose /api/ws.')
      }
      upstreamUrl.pathname = `${upstreamUrl.pathname.slice(0, -'/api/ws'.length)}${grant.endpoint.pathname}`
      for (const [key, value] of new URLSearchParams(grant.endpoint.search).entries()) {
        upstreamUrl.searchParams.append(key, value)
      }
      if (grant.profile) upstreamUrl.searchParams.set('profile', grant.profile)
      upstreamSocket = track(await connectUpstream(upstreamUrl, upstreamSetupTimeoutMs))
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
  MAX_CLIENT_MESSAGE_BYTES,
  TICKET_TTL_MS,
  buildUpgradeRequest,
  connectTls,
  createEvaWsRelay,
  normalizeEvaWsEndpoint,
  normalizeEvaWsProfile
}
